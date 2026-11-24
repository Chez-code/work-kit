# RUST-SETUP — installing and using the Rust box (rbox) on a machine

Instructions written so a Claude Code session on the target machine can apply
them directly. Paste-ready prompt blocks are at the bottom. Steps marked
**PERSON** can only be done by a human at their own terminal; a Claude session
that reaches one stops and hands it over.

`rbox` runs `cargo` inside a sandbox, so that code from crates.io (build
scripts, proc macros, tests, the programs you build) cannot read your files,
reach the network or write outside the build output. `RUST-STACK.md` explains
why and what the rules are. This file is how to install and operate it.

## Preflight

```bash
python3 --version      # 3.9 or newer, and it must be /usr/bin/python3
bwrap --version        # bubblewrap 0.8.0 or newer (Debian 12 and later have it)
bwrap --ro-bind / / --dev /dev --proc /proc --unshare-all true && echo box-ok
cc --version           # rustc needs a C linker and the C library's dev files
claude --version
```

If any of these is missing, **PERSON** installs them once:

```bash
sudo apt install bubblewrap gcc libc6-dev python3
```

`rbox doctor` (after install) repeats these checks and prints the exact line
to run. Two cases need more than a package:

- **`bwrap: setting up uid map: Permission denied`** — the distribution limits
  sandboxes (Ubuntu 24.04 and later do this through AppArmor). `rbox doctor`
  names the setting; changing it needs sudo, so it is a **PERSON** step.
- **Claude Code's own sandbox is switched on** — it blocks nested sandboxes by
  default, so `cargo` typed by Claude fails while the gate still works (hooks
  run outside that sandbox). List `rbox` and `cargo` under
  `sandbox.excludedCommands` in `~/.claude/settings.json`. Everything rbox
  runs is boxed by rbox itself.

## Fresh install

```bash
git clone https://github.com/Chez-code/work-kit ~/work-kit     # or: git -C ~/work-kit pull
~/work-kit/rustbox/install.sh
rbox doctor --online
rbox selftest
```

`install.sh` never uses sudo and touches only the home folder. The first
install can be run by a Claude session. Changing an installed machine from one
Rust version to another is a **PERSON** step: `install.sh` stops and says so.

| Path | What |
|---|---|
| `~/.local/share/rustbox/` | rbox itself, the Rust toolchain, the self-test crate |
| `~/.cache/rustbox/` | downloads, crate sources and build output, per project |
| `~/.local/state/rustbox/` | the crates a person approved on this machine |
| `~/.local/bin/rbox`, `~/.local/bin/cargo` | links to rbox |

The toolchain is the pinned Rust release in `rustbox/toolchain.sums`: about
130 MB of downloads from static.rust-lang.org, each checked against its
sha256, then installed inside a box with no network. There is no rustup.

**If the machine already has rustup or another cargo:** `install.sh` and
`rbox doctor` fail with `another cargo wins` when that cargo comes first on
`PATH`. Put `~/.local/bin` first, or remove the other one. A cargo that is
not rbox builds outside the box.

`rbox selftest` builds, tests and runs a deliberately hostile crate and checks
that it could not read your home folder, see your environment, reach the
network or any local socket, or write outside the build output. It should end
with `passed ... nothing leaked`.

Then, per repository:

```bash
~/work-kit/bootstrap.sh /path/to/repo     # the gate, the guards, the watcher
cd /path/to/repo
rbox sync                                 # download the crates Cargo.lock names; nothing runs
rbox pending                              # what is waiting for approval
```

**PERSON**, in their own terminal pane (not `!` inside Claude):

```bash
cd /path/to/repo && rbox allow
```

then `rbox warm` pre-builds, so the first gate run is not a cold build.

Where the hooks live follows `WORK-SETUP.md` ("Fresh install" there: move the
hooks block to `.claude/settings.local.json` in a repo you share). This
release adds two things to that block, and both must end up in whichever
settings file holds the hooks: the `PreToolUse` entry for `policy_guard.py`
and the `permissions.deny` list. `kit/.claude/settings.json` shows them in
place.

rbox adds nothing to the repository: no folder, no config file, no ignore
lines. Crate sources and build output stay under `~/.cache/rustbox/`.

### CLAUDE.md on a Rust repo

Replace the template's Commands section with:

```markdown
## Commands
- Check: `cargo check --workspace --all-targets`
- Lint: `cargo clippy --workspace --all-targets -- -D warnings` / format: `cargo fmt`
- Test: `cargo test --workspace` (live tests are `#[ignore]`: `rbox --net cargo test -- --ignored`)
- Run: `rbox run -- <program>`; never run a built program any other way
- `cargo` here is rbox: it is sandboxed. `rbox status` shows where crate sources are.
- New or changed crates need the user's approval (`rbox allow`, theirs alone).
  If a build says crates await approval, say which and why, then stop.
```

## Day to day

`cargo` is rbox, so the usual commands work as usual:

| You type | What happens |
|---|---|
| `cargo build` `check` `clippy` `test` `run` `bench` `doc` | build box: no network, project read-only, crates from rbox's downloaded copy |
| `cargo fmt` | formats in place; no network |
| `cargo add` `remove` `update` | fetch box: only cargo runs, with a 7-day cooldown on new versions; then the crates are downloaded |
| `rbox sync` | download what `Cargo.lock` names (after a pull, a checkout or a hand edit) |
| `rbox pending` | the crates waiting for approval, with age and what runs at build time |
| `rbox status` | project root, where the crate sources and build output are, what is hidden in the box |
| `rbox run -- NAME [ARGS]` | run a built program in the box (`--release`, `--rw FOLDER`, `--env NAME`) |
| `rbox warm` | pre-build after dependency changes |
| `rbox new NAME` | a new project with overflow checks, `panic = "abort"` and the lints |
| `rbox clean` | empty this project's build output |

Refused, and what to do instead:

| Refused | Instead |
|---|---|
| `cargo install`, `publish`, `login` | not done from the box |
| `cargo fix`, `clippy --fix` | apply the change with an editor |
| `cargo init` | `rbox new NAME` |
| `--config`, `-Z`, `--target-dir`, `+toolchain` | they would change rbox's rules |
| `./target/...` | there is no `target/` in the project; use `rbox run -- NAME` |

Things that differ from plain cargo:

- **The project is read-only while building.** A build script or test that
  writes into the source tree fails. Write under the build output
  (`OUT_DIR`, `CARGO_TARGET_TMPDIR`) or `/tmp` instead.
- **`.git`, `.claude`, `.mcp.json`, `.env`, `.env.*`, `.envrc`, `.netrc`, `.npmrc`
  and `.pypirc` are hidden in the box**, wherever they are in the project,
  including under a folder you make writable with `--rw`. Folders a Rust build
  has no use for are hidden whole: `node_modules`, `__pycache__`, Python
  virtualenvs, and cache folders such as an old `target/` left by plain cargo.
  Every other folder is searched, whatever it is called. A project (or a
  `--rw` folder) with more than two million files is not searched in part:
  rbox does not run there, and it warns from 500,000 on. Keep bulk data in a
  folder of its own. Keep keys out of the project tree anyway and pass them
  at run time: `rbox run --env API_KEY -- NAME`.
- **To read a dependency's source**, look under the "crate sources" path that
  `rbox status` prints. Reading is all a session may do there: the guard
  refuses any command that mentions rbox's cache and does more than look
  (`cat`, `grep`, `rg`, `ls`, `find`, `head`, `wc`, `jq`). Those files are
  what the person approved.
- **A built program leaves the box only through `rbox export NAME --to FOLDER`**,
  which is a **PERSON** step and prints the file's sha256.
- **`rbox shell` opens a shell inside the build box**, to look at what the box
  can see. It is a **PERSON** step and needs every crate approved: cargo typed
  in that shell runs without the settings rbox pins on its own cargo commands.
- **A boxed command stops when the shell that started it exits.** That is
  what keeps a killed session from leaving builds behind. Run a long job in
  tmux, or as a service.

## Adding or updating a crate

1. `cargo add NAME` (or `cargo update`). This downloads; nothing runs.
2. The next build stops with `rbox: waiting: N crates await approval`. The gate
   does not block on this: it tells the operator the Rust checks did not run,
   and the watcher shows `UNVERIFIED` until they run again.
3. **PERSON**: `rbox allow`, in their own terminal. It shows every pending
   crate file: version (and the version it replaces), how long ago it was
   published, whether it has a build script or is a proc macro, and which of
   your dependencies pulls it in. Crates that run code at build time are
   listed first and confirmed separately. A crate that runs code and is
   younger than 30 days is confirmed by typing its name, and so is a version
   its authors have yanked. rbox asks crates.io again about every pending
   crate at that moment, so approving needs crates.io to be reachable: a
   version that was removed since the download is refused.
4. A version younger than 7 days cannot be approved. Wait, or for a security
   fix **PERSON** runs `rbox urgent NAME@VERSION`, which pulls that one
   version past the cooldown and leaves a dated note.

What to look at in the table: anything you did not expect, anything new that
runs code at build time, anything published very recently, anything marked
`YANKED`, and any row under "LOCAL CODE STANDING IN FOR A CRATE" (a `[patch]`
that replaces a crates.io crate with code from the repository).

Approval is per crate file (name, version and checksum) and per machine. A
file approved once is approved for every project on that machine. Nothing in
a repository can approve a crate: a `Cargo.lock` that arrives through a pull
waits for approval like any other change. Approvals do not travel between
machines.

## Network for tests and programs

By default nothing in the box has a network. For live tests and for programs
that talk to an API:

```bash
rbox net list                               # the hosts this project may reach
rbox net allow api.example.com:443          # PERSON, own terminal
rbox --net cargo test -- --ignored          # live tests
rbox run --net --env API_KEY -- recorder    # a program
```

- The box still has no network of its own. rbox runs a proxy outside the box
  and offers it inside as `HTTPS_PROXY`. Only `CONNECT` to a listed
  `host:port` gets through; every connection is logged in the project's state
  folder (`net.log`).
- **Compiling never gets the network, even with `--net`.** `rbox --net cargo
  test` first builds in the ordinary box with no network, then runs only the
  finished test programs with the proxy. `rbox --net cargo run` does the same
  for one binary. Build scripts and proc macros never see the proxy. Doc
  tests are not run with `--net`.
- **The code has to use the proxy.** `reqwest` does on its own. A raw TCP or
  WebSocket client needs a short `CONNECT` step first; `RUST-STACK.md` has it.
  TCP only.
- A listed name that resolves to a local or private address is refused. To
  reach a service on this machine or network, list its address literally
  (`127.0.0.1:8765`).
- `build`, `check`, `clippy`, `doc` and `bench` refuse `--net`, and the gate's
  `cargo test` runs without it. Mark live tests `#[ignore]`.
- Do not list general-purpose hosts (code hosting, paste sites, object
  storage): a hostile crate could use them to send data out.

## What the gate does in a Rust repo

The Rust lane is on when the repo has a `Cargo.toml` at its root, or in a
folder one level down (a C# or Python repo that keeps its Rust in a
subfolder). cargo then runs inside that folder. With more than three such
folders the gate skips visibly and asks for `GATE_RUST_DIR=<folder>` as a
prefix in the hook command. The policy guard is active under the same test.

| When | Runs |
|---|---|
| after each edit of `.rs`, `Cargo.toml`, `Cargo.lock` | `cargo check --workspace --all-targets` |
| when Claude stops | `cargo fmt --check`, then `cargo clippy -D warnings`, then `cargo test --workspace` |

| rbox says | The gate |
|---|---|
| `refused` (exit 3): a rule said no, for example crates not downloaded yet | blocks, and the message says what to run |
| `cannot-run` (exit 4): no sandbox or no toolchain on this machine | visible skip: nothing in Rust is being checked |
| `waiting` (exit 5): crates await approval | does not block; tells the operator; formatting is still enforced |

A repository can have more than one lane. In a repo with both `Cargo.toml`
and `pyproject.toml` a `.py` edit runs the Python lane and a `.rs` edit the
Rust lane; in a repo with a solution file and a `Cargo.toml`, `.cs` edits
build with dotnet and `.rs` edits check with cargo. Stop runs every lane the
repo has, inside the one `GATE_TIMEOUT` budget.

## Updating (after new kit commits)

```bash
git -C ~/work-kit pull
~/work-kit/rustbox/install.sh          # updates rbox; a changed Rust pin needs a PERSON to run this
~/work-kit/sync.sh /path/to/repo
rbox doctor && rbox selftest
```

## Smoke test

On the machine:

```bash
rbox doctor --online && rbox selftest
```

The kit's own tests, from the kit checkout:

```bash
cd ~/work-kit
python3 -m unittest discover tests && tests/smoke.sh    # no toolchain needed
python3 tests/rust/test_online.py                       # real cargo, real crates from crates.io
python3 tests/rust/mutation.py                          # weakens the box on purpose: the self-test must notice
```

In a Rust repo, the gate by hand:

```bash
echo '{"cwd":"'"$PWD"'","tool_input":{"file_path":"src/main.rs"}}' | python3 .claude/hooks/gate.py; echo "exit=$?"
echo '{"cwd":"'"$PWD"'"}' | python3 .claude/hooks/gate.py --stop; echo "exit=$?"
echo '{"cwd":"'"$PWD"'","tool_name":"Bash","tool_input":{"command":"rbox allow"}}' | python3 .claude/hooks/policy_guard.py; echo "exit=$? (2 expected: refused)"
```

Expect `exit=0` on a green repo. A `systemMessage` line means the gate skipped
or is waiting for an approval; read it, it says which.

## Limits

- Anything running as you outside the box, including a hijacked Claude
  session, can rewrite rbox's files. The terminal check, the guard hook and
  the deny rules stop accidents and over-eager sessions; Claude Code's own
  permission prompts are the wall.
- The box covers building, testing and `rbox run`. An exported program runs
  wherever you start it.
- crates.io only: git dependencies and other registries are refused.
- Local path crates are not checksummed. A `[patch]` to local code is shown
  and has to be confirmed, per project.
- `--net` needs proxy-aware code, is TCP only, and does not chain through an
  upstream company proxy.
- A release build is linked against this machine's C library.
- Not attempted: kernel exploits.

## Paste-ready prompts for the machine's Claude

Fresh install:

> Read ~/work-kit/RUST-SETUP.md and apply the preflight and fresh-install
> steps on this machine, then set up <repo path>. Stop and give me the exact
> command whenever a step is marked PERSON or needs sudo. Finish with
> `rbox doctor --online`, `rbox selftest` and `rbox pending`, and show me
> their output.

Update:

> Read ~/work-kit/RUST-SETUP.md and apply the update steps: pull the kit, run
> rustbox/install.sh and sync.sh for <repo path>, show me any .prev diffs,
> merge the PreToolUse entry and the permissions.deny list into
> settings.local.json if the hooks live there, then run `rbox doctor` and
> `rbox selftest`.
