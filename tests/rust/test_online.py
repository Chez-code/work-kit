#!/usr/bin/env python3
"""End-to-end tests for rbox with the real toolchain and real crates from crates.io.

Needs: rustbox/install.sh has been run, and crates.io is reachable.
Run from the kit checkout:  python3 tests/rust/test_online.py

Everything happens in a separate test world (RBOX_TEST_BASE): its own approvals
and its own downloads, so nothing here touches this machine's confirmed set.
"Approving" in these tests means writing that test world's confirmed file
directly; the real `rbox allow` needs a person at a terminal, and the tests
check that it refuses anything else.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
KIT = Path(__file__).resolve().parent.parent.parent
RBOX = str(KIT / "rustbox" / "rbox.py")
sys.path.insert(0, str(KIT / "rustbox"))
import rbox  # noqa: E402

UNDER_CLAUDE = any(os.environ.get(m) for m in rbox.CLAUDE_MARKERS)
MAIN_RS = """use serde::Serialize;

#[derive(Serialize)]
struct Tick {
    px: i64,
    qty: i64,
}

fn main() {
    let tick = Tick { px: 101, qty: 5 };
    match serde_json::to_string(&tick) {
        Ok(text) => println!("{text}"),
        Err(err) => eprintln!("{err}"),
    }
}
"""


class Online(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            rbox.Dirs({}).toolchain()
        except rbox.Stop as stop:
            raise unittest.SkipTest(stop.message)
        if "error" in rbox.fetch_index("cfg-if"):
            raise unittest.SkipTest("crates.io index is not reachable")
        cls.tmp = tempfile.mkdtemp(prefix="rbox-online-")
        cls.base = Path(cls.tmp).resolve()
        cls.world = cls.base / "world"
        cls.env = dict(os.environ, RBOX_TEST_BASE=str(cls.world))
        cls.dirs = rbox.Dirs({"RBOX_TEST_BASE": str(cls.world)})
        # one project with real dependencies, reused by most tests
        cls.proj = cls.new_project("dep")
        (cls.proj / "src" / "main.rs").write_text(MAIN_RS)
        code, out = cls.run_rbox(["cargo", "add", "serde", "--features", "derive"], cls.proj)
        assert code == 0, out
        code, out = cls.run_rbox(["cargo", "add", "serde_json"], cls.proj)
        assert code == 0, out

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "tmp", None):
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers

    @classmethod
    def run_rbox(cls, args: list, cwd: Path, env: dict | None = None, stdin=subprocess.DEVNULL) -> tuple:
        done = subprocess.run([RBOX, *args], cwd=str(cwd), env=env or cls.env, stdin=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
        return done.returncode, done.stdout

    @classmethod
    def new_project(cls, name: str) -> Path:
        parent = cls.base / ("p-" + name)
        parent.mkdir()
        code, out = cls.run_rbox(["new", name], parent)
        assert code == 0, out
        return parent / name

    @classmethod
    def approve_everything(cls, cwd: Path) -> int:
        """Test-world approval: copy what is approvable into this world's confirmed file."""
        code, out = cls.run_rbox(["allow", "--tsv"], cwd)
        assert code == 0, out
        lines = [line.split("\t") for line in out.splitlines() if "\t" in line]
        rbox.add_confirmed(cls.dirs, [(f[0], f[1], f[2]) for f in lines if f[5] == "approvable"])
        return len(lines)

    def cache_of(self, project: Path) -> Path:
        return rbox.Project(self.dirs, project.resolve()).cache

    def state_of(self, project: Path) -> Path:
        return rbox.Project(self.dirs, project.resolve()).state

    # -------------------------------------------------------------- tests

    def test_01_new_crates_pause_the_build(self):
        code, out = self.run_rbox(["cargo", "build"], self.proj)
        self.assertEqual(code, rbox.EXIT_WAITING, out)
        self.assertIn("await approval", out)
        self.assertIn("serde_json", out)

    def test_02_review_table_has_what_a_person_needs(self):
        code, out = self.run_rbox(["allow", "--show"], self.proj)
        self.assertEqual(code, 0, out)
        self.assertIn("RUNS CODE AT BUILD TIME", out)
        self.assertRegex(out, r"serde_derive\s+\S+ \(new\)\s+published .* ago\s+proc macro\s+via serde")
        self.assertRegex(out, r"proc-macro2\s+\S+ \(new\)\s+published .* ago\s+build script")
        self.assertIn("OTHER", out)

    def test_03_approval_needs_a_person(self):
        code, out = self.run_rbox(["allow"], self.proj)
        self.assertEqual(code, rbox.EXIT_REFUSED, out)
        self.assertIn("typed by a person", out)
        for command in (["urgent", "serde@1.0.0"], ["export", "dep", "--to", str(self.base / "out")]):
            code, out = self.run_rbox(command, self.proj)
            self.assertEqual(code, rbox.EXIT_REFUSED, out)
            self.assertIn("typed by a person", out)
        self.assertFalse((self.base / "out").exists())

    @unittest.skipUnless(UNDER_CLAUDE and shutil.which("script"), "only meaningful inside a Claude session")
    def test_04_a_faked_terminal_does_not_help(self):
        clean = {k: v for k, v in self.env.items() if k not in rbox.CLAUDE_MARKERS}
        done = subprocess.run(["script", "-qec", f"{RBOX} allow", "/dev/null"], cwd=str(self.proj), env=clean,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=30)
        self.assertIn("parent process is a Claude session", done.stdout)
        self.assertEqual(rbox.load_confirmed(self.dirs), {})

    def test_05_confirmed_crates_build_offline_and_run(self):
        self.assertEqual(self.approve_everything(self.proj), 11)
        code, out = self.run_rbox(["cargo", "build"], self.proj)
        self.assertEqual(code, 0, out)
        code, out = self.run_rbox(["cargo", "run", "-q"], self.proj)
        self.assertIn('{"px":101,"qty":5}', out)
        code, out = self.run_rbox(["run", "--", "dep"], self.proj)
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, '{"px":101,"qty":5}'))
        code, out = self.run_rbox(["cargo", "clippy", "--all-targets", "--", "-D", "warnings"], self.proj)
        self.assertEqual(code, 0, out)

    def test_06_nothing_of_rbox_lands_in_the_project(self):
        names = sorted(p.name for p in self.proj.iterdir())
        self.assertEqual(names, ["Cargo.lock", "Cargo.toml", "clippy.toml", "src"])
        self.assertTrue((self.cache_of(self.proj) / "target" / "debug" / "dep").is_file())
        self.assertTrue((self.cache_of(self.proj) / "vendor").is_dir())

    def test_07_same_crate_files_in_a_second_project_build_without_asking(self):
        second = self.new_project("second")
        code, out = self.run_rbox(["cargo", "add", "serde_json"], second)
        self.assertEqual(code, 0, out)
        code, out = self.run_rbox(["cargo", "build"], second)
        self.assertEqual(code, 0, out)
        self.assertNotEqual(self.cache_of(second), self.cache_of(self.proj))

    def test_08_a_version_change_asks_again(self):
        versions = rbox.fetch_index("itoa")
        current = [r for r in rbox.read_json(self.state_of(self.proj) / "meta.json", {})["rows"] if r["name"] == "itoa"][0]
        older = sorted(v for v, info in versions.items()
                       if v.startswith("1.0.") and v != current["version"] and not info["yanked"]
                       and (rbox.age_days(info["pubtime"]) or 0) > 60)[-1]
        code, out = self.run_rbox(["cargo", "update", "itoa", "--precise", older], self.proj)
        self.assertEqual(code, 0, out)
        code, out = self.run_rbox(["cargo", "build"], self.proj)
        self.assertEqual(code, rbox.EXIT_WAITING, out)
        self.assertIn(f"itoa {older}", out)
        code, out = self.run_rbox(["allow", "--show"], self.proj)
        self.assertIn(f"{current['version']} -> {older}", out)
        self.approve_everything(self.proj)
        code, out = self.run_rbox(["cargo", "build"], self.proj)
        self.assertEqual(code, 0, out)

    def test_09_a_lockfile_that_arrives_from_outside_cannot_approve_itself(self):
        # a "teammate" clone picks an older memchr; their Cargo.lock then lands here, as a pull would bring it
        teammate = self.base / "teammate"
        shutil.copytree(self.proj, teammate)
        versions = rbox.fetch_index("memchr")
        older = sorted(v for v, info in versions.items() if v.startswith("2.7.") and not info["yanked"])[-1]
        code, out = self.run_rbox(["cargo", "update", "memchr", "--precise", older], teammate)
        self.assertEqual(code, 0, out)
        before = (self.proj / "Cargo.lock").read_text()
        try:
            shutil.copy(teammate / "Cargo.lock", self.proj / "Cargo.lock")
            (self.proj / "confirmed").write_text(f"memchr {older} {'0' * 64}\n")  # a file in the repo proves nothing
            code, out = self.run_rbox(["cargo", "build"], self.proj)
            self.assertEqual(code, rbox.EXIT_REFUSED, out)
            self.assertIn("rbox sync", out)
            code, out = self.run_rbox(["sync"], self.proj)
            self.assertEqual(code, 0, out)
            code, out = self.run_rbox(["cargo", "build"], self.proj)
            self.assertEqual(code, rbox.EXIT_WAITING, out)
            self.assertIn(f"memchr {older}", out)
        finally:
            (self.proj / "confirmed").unlink(missing_ok=True)
            (self.proj / "Cargo.lock").write_text(before)
            self.run_rbox(["sync"], self.proj)

    def test_10_a_patch_to_local_code_is_shown_and_waits(self):
        vendor = self.cache_of(self.proj) / "vendor"
        source = sorted(vendor.glob("itoa-*"))[0]
        local = self.proj / "third_party" / "itoa"
        manifest = (self.proj / "Cargo.toml").read_text()
        lock = (self.proj / "Cargo.lock").read_text()
        try:
            shutil.copytree(source, local)
            (local / ".cargo-checksum.json").unlink()
            (self.proj / "Cargo.toml").write_text(manifest + '\n[patch.crates-io]\nitoa = { path = "third_party/itoa" }\n')
            code, out = self.run_rbox(["sync"], self.proj)
            self.assertEqual(code, 0, out)
            code, out = self.run_rbox(["cargo", "build"], self.proj)
            self.assertEqual(code, rbox.EXIT_WAITING, out)
            self.assertIn("itoa (local: third_party/itoa)", out)
            code, out = self.run_rbox(["allow", "--show"], self.proj)
            self.assertIn("LOCAL CODE STANDING IN FOR A CRATE (1)", out)
            self.assertIn("replaces a crates.io crate", out)
            rbox.add_pairs(self.state_of(self.proj) / "local-ok", [("itoa", "third_party/itoa")], "# test\n")
            code, out = self.run_rbox(["cargo", "build"], self.proj)
            self.assertEqual(code, 0, out)
        finally:
            shutil.rmtree(self.proj / "third_party", ignore_errors=True)
            (self.proj / "Cargo.toml").write_text(manifest)
            (self.proj / "Cargo.lock").write_text(lock)
            (self.state_of(self.proj) / "local-ok").unlink(missing_ok=True)
            self.run_rbox(["sync"], self.proj)

    def test_11_project_cargo_config_cannot_run_a_program_or_drop_the_cooldown(self):
        proj = self.new_project("tricks")
        member = proj / "crates" / "m"
        (member / "src").mkdir(parents=True)
        (member / "Cargo.toml").write_text('[package]\nname = "m"\nversion = "0.1.0"\nedition = "2024"\n')
        (member / "src" / "lib.rs").write_text("pub fn m() {}\n")
        (proj / "Cargo.toml").write_text((proj / "Cargo.toml").read_text().replace(
            "[package]", '[workspace]\nmembers = ["crates/m"]\n\n[package]', 1))
        marker = proj / "WRAPPER_RAN"
        wrapper = proj / "wrap.sh"
        wrapper.write_text(f'#!/bin/sh\necho ran >> "{marker}" 2>/dev/null\necho ran >> /rbox/target/WRAPPER_RAN 2>/dev/null\nexec "$@"\n')
        wrapper.chmod(0o755)
        hostile = (f'[build]\nrustc-wrapper = "{wrapper}"\n[alias]\nfmt = "version"\n'
                   '[registries.crates-io]\nmin-publish-age = "0"\n[resolver]\nincompatible-publish-age = "allow"\n')
        (proj / ".cargo").mkdir()
        (proj / ".cargo" / "config.toml").write_text(hostile)
        (member / ".cargo").mkdir()
        (member / ".cargo" / "config").write_text(hostile)  # the legacy file name, one level down
        self.run_rbox(["sync"], proj)
        for cwd in (proj, member):
            for command in (["cargo", "update"], ["cargo", "fetch"], ["cargo", "generate-lockfile"],
                            ["cargo", "add", "cfg-if"], ["cargo", "remove", "cfg-if"], ["sync"]):
                code, out = self.run_rbox(command, cwd)
                self.assertEqual(code, 0, f"{command} in {cwd}: {out}")
                self.assertFalse(marker.exists(), f"{command} in {cwd} ran the project's wrapper")
            code, out = self.run_rbox(["cargo", "fmt", "--check"], cwd)
            self.assertEqual(code, 0, out)
            self.assertNotIn("cargo 1.", out, "the fmt alias from the project config took effect")
        # positive control: the same wrapper does run in the build box, where project code is allowed to run
        code, out = self.run_rbox(["cargo", "check"], proj)
        self.assertEqual(code, 0, out)
        self.assertTrue((self.cache_of(proj) / "target" / "WRAPPER_RAN").exists(), "control: wrapper never ran at all")
        self.assertFalse(marker.exists(), "the build box wrote into the project")

    def test_12_cooldown_is_enforced_whatever_the_project_says(self):
        proj = self.new_project("cool")
        (proj / ".cargo").mkdir()
        (proj / ".cargo" / "config.toml").write_text(
            '[registries.crates-io]\nmin-publish-age = "0"\n[resolver]\nincompatible-publish-age = "allow"\n')
        before = (proj / "Cargo.toml").read_text()
        script = (
            "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]); import rbox, os\n"
            "rbox.COOLDOWN_DAYS = 365000\n"
            "try:\n    code = rbox.cmd_cargo(rbox.Dirs(dict(os.environ)), ['add', 'cfg-if'], False, {})\n"
            "except rbox.Stop as stop:\n    code = stop.code\nsys.exit(code)\n")
        done = subprocess.run([sys.executable, "-c", script, str(KIT / "rustbox")], cwd=str(proj), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("too new", done.stdout)
        self.assertEqual((proj / "Cargo.toml").read_text(), before)

    def test_13_cargo_holds_back_a_young_transitive_crate(self):
        # cargo's own behaviour, on real publish dates: a direct crate that depends on libc,
        # with the age set between libc's two newest releases
        libc = {v: info for v, info in rbox.fetch_index("libc").items()
                if v.startswith("0.2.") and not info["yanked"]}
        newest, previous = sorted(libc, key=lambda v: libc[v]["pubtime"])[-1], sorted(libc, key=lambda v: libc[v]["pubtime"])[-2]
        age = int(((rbox.age_days(libc[newest]["pubtime"]) or 0) + (rbox.age_days(libc[previous]["pubtime"]) or 0)) / 2)
        if age < 1 or int(rbox.age_days(libc[newest]["pubtime"]) or 0) == int(rbox.age_days(libc[previous]["pubtime"]) or 0):
            self.skipTest("libc's two newest releases are too close together to test between them")
        proj = self.new_project("hold")
        (proj / "Cargo.toml").write_text((proj / "Cargo.toml").read_text().replace("[dependencies]", '[dependencies]\nnum_cpus = "1"'))
        (proj / "Cargo.lock").unlink()
        script = (
            "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, sys.argv[1]); import rbox, os\n"
            f"rbox.COOLDOWN_DAYS = {age}\n"
            "dirs = rbox.Dirs(dict(os.environ)); bwrap = rbox.find_bwrap(); tc = dirs.toolchain()\n"
            "p = rbox.Project(dirs, rbox.Path.cwd().resolve())\n"
            "sys.exit(rbox.run_fetch(dirs, bwrap, tc, p, p.root, [rbox.CARGO, 'generate-lockfile', *rbox.cooldown_pins()], network=True))\n")
        done = subprocess.run([sys.executable, "-c", script, str(KIT / "rustbox")], cwd=str(proj), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout)
        lock = (proj / "Cargo.lock").read_text()
        self.assertIn(f'name = "libc"\nversion = "{previous}"', lock, done.stdout)
        self.assertNotIn(f'version = "{newest}"', lock)

    def test_14_tampering_with_downloaded_crates_is_refused(self):
        import json
        proj = self.new_project("tamper")
        self.run_rbox(["cargo", "add", "itoa"], proj)
        self.approve_everything(proj)
        self.assertEqual(self.run_rbox(["cargo", "build"], proj)[0], 0)
        crate = sorted((self.cache_of(proj) / "vendor").glob("itoa-*"))[0]
        checksum = crate / ".cargo-checksum.json"
        original = checksum.read_text()
        data = json.loads(original)
        data["package"] = "0" * 64
        checksum.write_text(json.dumps(data))
        code, out = self.run_rbox(["cargo", "build"], proj)
        self.assertNotEqual(code, 0, out)  # the recorded checksum no longer matches what was approved
        checksum.write_text(original)
        source = crate / "src" / "lib.rs"
        text = source.read_text()
        source.write_text(text + "\n// tampered\n")
        self.run_rbox(["clean"], proj)
        code, out = self.run_rbox(["cargo", "build"], proj)
        self.assertNotEqual(code, 0, out)
        self.assertIn("checksum", out)
        source.write_text(text)
        self.assertEqual(self.run_rbox(["cargo", "build"], proj)[0], 0)

    def test_15_rule_changing_commands_are_refused(self):
        for command in (["install", "ripgrep"], ["build", "--config", "x=1"], ["+nightly", "build"],
                        ["clippy", "--fix"], ["build", "-Zunstable-options"], ["publish"], ["init"],
                        ["add", "x", "--git", "https://example.invalid/x"], ["nextest", "run"],
                        ["build", "--target-dir", "/tmp/elsewhere"]):
            code, out = self.run_rbox(["cargo", *command], self.proj)
            self.assertEqual(code, rbox.EXIT_REFUSED, f"{command}: {out}")

    def test_16_network_flag_is_not_for_builds(self):
        for command in (["build"], ["check"], ["clippy"], ["doc"]):
            code, out = self.run_rbox(["--net", "cargo", *command], self.proj)
            self.assertEqual(code, rbox.EXIT_REFUSED, out)

    def test_17_new_projects_get_the_safety_settings_and_work_at_once(self):
        proj = self.new_project("fresh")
        manifest = (proj / "Cargo.toml").read_text()
        for line in ("overflow-checks = true", 'panic = "abort"', 'unsafe_code = "forbid"', 'unwrap_used = "deny"'):
            self.assertIn(line, manifest)
        self.assertEqual(self.run_rbox(["cargo", "check"], proj)[0], 0)
        self.assertEqual(self.run_rbox(["cargo", "clippy", "--all-targets", "--", "-D", "warnings"], proj)[0], 0)
        (proj / "src" / "main.rs").write_text("fn main() {\n    let x: Option<u8> = std::env::args().next().map(|_| 1);\n    println!(\"{}\", x.unwrap());\n}\n")
        code, out = self.run_rbox(["cargo", "clippy", "--", "-D", "warnings"], proj)
        self.assertNotEqual(code, 0, out)
        self.assertIn("unwrap", out)

    def test_18_new_does_not_expose_the_folder_it_runs_in(self):
        parent = self.base / "many"
        parent.mkdir()
        (parent / "sibling-secret.txt").write_text("private")
        code, out = self.run_rbox(["new", "solo"], parent)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in parent.iterdir()), ["sibling-secret.txt", "solo"])
        self.assertEqual((parent / "sibling-secret.txt").read_text(), "private")

    def test_19_root_comes_from_cargo(self):
        proj = self.new_project("ws")
        member = proj / "crates" / "m"
        (member / "src").mkdir(parents=True)
        (member / "Cargo.toml").write_text('[package]\nname = "m"\nversion = "0.1.0"\nedition = "2024"\n')
        (member / "src" / "lib.rs").write_text("")
        (proj / "Cargo.toml").write_text((proj / "Cargo.toml").read_text().replace(
            "[package]", '[workspace]\nmembers = ["crates/m"]\n\n[package]', 1))
        self.run_rbox(["sync"], proj)
        code, out = self.run_rbox(["status"], member / "src")
        self.assertRegex(out, rf"project\s+{proj}\n")
        code, out = self.run_rbox(["cargo", "check", "--manifest-path", str(member / "Cargo.toml")], self.base)
        self.assertEqual(code, 0, out)
        code, out = self.run_rbox(["cargo", "check"], self.base)
        self.assertEqual(code, rbox.EXIT_REFUSED, out)

    def test_20_run_folder_rules(self):
        data = self.proj / "data"
        data.mkdir(exist_ok=True)
        code, out = self.run_rbox(["run", "--rw", "data", "--", "dep"], self.proj)
        self.assertEqual(code, 0, out)
        (self.proj / ".git").mkdir(exist_ok=True)
        code, out = self.run_rbox(["run", "--rw", ".git", "--", "dep"], self.proj)
        self.assertEqual(code, rbox.EXIT_REFUSED, out)
        code, out = self.run_rbox(["run", "--rw", str(self.base), "--", "dep"], self.proj)
        self.assertEqual(code, rbox.EXIT_REFUSED, out)  # outside the project: a person only
        code, out = self.run_rbox(["run", "--", "../../etc/passwd"], self.proj)
        self.assertEqual(code, rbox.EXIT_REFUSED, out)
        shutil.rmtree(self.proj / ".git")
        shutil.rmtree(data)

    def test_21_another_cargo_first_on_path_fails_the_machine_check(self):
        fake = self.base / "fakecargo"
        fake.mkdir()
        (fake / "cargo").write_text("#!/bin/sh\necho not boxed\n")
        (fake / "cargo").chmod(0o755)
        env = dict(self.env, PATH=str(fake) + os.pathsep + self.env.get("PATH", ""))
        code, out = self.run_rbox(["doctor"], self.base, env=env)
        self.assertEqual(code, rbox.EXIT_CANNOT_RUN, out)
        self.assertIn("another cargo wins", out)

    def test_22_killing_the_caller_leaves_nothing_running(self):
        proj = self.new_project("sleepy")
        (proj / "src" / "main.rs").write_text(
            "fn main() {\n    std::thread::sleep(std::time::Duration::from_secs(300));\n}\n")
        self.assertEqual(self.run_rbox(["cargo", "build"], proj)[0], 0)
        tag = f"rbox-kill-test-{os.getpid()}"
        # caller -> rbox (which becomes bwrap) -> the program, like the gate calling rbox
        caller = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, sys; subprocess.run(sys.argv[1:])", RBOX, "run", "--", "sleepy", tag],
            cwd=str(proj), env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def running() -> bool:
            # matches bwrap and the program inside it, not the caller (whose arguments have no "debug/")
            return subprocess.run(["pgrep", "-f", f"debug/sleepy {tag}"], stdout=subprocess.DEVNULL).returncode == 0

        for _ in range(50):
            if running():
                break
            time.sleep(0.1)
        self.assertTrue(running(), "control: the program never started")
        os.kill(caller.pid, signal.SIGKILL)
        caller.wait()
        for _ in range(50):
            if not running():
                break
            time.sleep(0.1)
        self.assertFalse(running(), "the boxed program outlived its caller")


if __name__ == "__main__":
    unittest.main(verbosity=2)
