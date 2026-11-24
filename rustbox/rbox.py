#!/usr/bin/python3 -I
"""rbox: build, test and run Rust inside a bubblewrap box. Stdlib only.

Two boxes:
  build box  crate code may run. No network, project read-only, crates only
             from the vendored copy, throwaway cargo home.
  fetch box  only cargo and rustfmt run. Project writable, project cargo
             config hidden, cooldown forced. Network only for downloads.

Nothing rbox-specific lives in the project. Per project (keyed by a hash of
its path) the vendored crates and the build output live under the cache dir
and appear inside the box at /rbox/vendor and /rbox/target.

Exit codes of rbox itself:
  3  refused by a rule; the caller can fix it
  4  the box cannot run on this machine
  5  waiting for a person (crates await approval)
Anything else is the exit code of the command that ran in the box.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 9):
    sys.stderr.write("rbox: cannot-run: needs Python 3.9 or newer\n")
    sys.exit(4)

import concurrent.futures
import datetime
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

EXIT_REFUSED = 3
EXIT_CANNOT_RUN = 4
EXIT_WAITING = 5

COOLDOWN_DAYS = 7  # a crate version younger than this is never approved without `rbox urgent`
YOUNG_DAYS = 30  # a code-running crate younger than this must be confirmed by name
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
INDEX_URL = "https://index.crates.io/"
CLAUDE_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_ENTRYPOINT")
BWRAP_PLACES = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap")

# cargo subcommands by box. Aliases b/c/t/r/d are cargo's own.
BUILD_CMDS = {
    "build", "b", "check", "c", "clippy", "test", "t", "bench", "run", "r", "doc", "d",
    "rustc", "rustdoc", "metadata", "tree", "pkgid", "locate-project",
}
NET_OK_CMDS = {"test", "t", "run", "r", "bench"}  # the only cargo commands `--net` applies to
FETCH_NET_CMDS = {"add", "remove", "rm", "update", "generate-lockfile", "search"}
SYNC_CMDS = {"fetch", "vendor"}  # both mean: make the vendored copy match the lockfile
FETCH_LOCAL_CMDS = {"fmt"}
NO_PROJECT_CMDS = {"version", "help", "--version", "-V", "--help", "-h", "--list"}
REFUSED_CMDS = {
    "install": "installing programs from crates is outside the box in v1",
    "uninstall": "nothing is installed through rbox",
    "publish": "publishing is not done from the box",
    "package": "packaging is not done from the box",
    "login": "rbox keeps no registry credentials",
    "logout": "rbox keeps no registry credentials",
    "owner": "registry ownership is not managed from the box",
    "yank": "yanking is not done from the box",
    "fix": "it compiles crate code and rewrites your sources in one step; apply fixes with an editor",
    "init": "use `rbox new NAME` (it creates the project in an empty folder first)",
    "new": "use `rbox new NAME`",
}
LEADING_OK = {"-v", "-vv", "-q", "--verbose", "--quiet", "--locked", "--offline", "--frozen"}
REFUSED_FLAGS = ("--config", "-Z", "--target-dir", "-C")
PASS_ENV = ("RUST_BACKTRACE", "RUST_LOG", "RUSTFLAGS", "RUSTDOCFLAGS", "NO_COLOR", "CARGO_TERM_COLOR")
PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "all_proxy", "no_proxy")
HIDE_DIRS = (".git", ".claude")
HIDE_FILES = (".mcp.json", ".env")
WALK_PRUNE = {"target", "vendor", "node_modules", ".venv", "__pycache__"}
WALK_LIMIT = 200000


# ------------------------------------------------------------------ basics


class Stop(Exception):
    """Ends rbox with one line on stderr and a meaningful exit code."""

    def __init__(self, code: int, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


def refuse(message: str, hint: str = "") -> Stop:
    return Stop(EXIT_REFUSED, message, hint)


def cannot_run(message: str, hint: str = "") -> Stop:
    return Stop(EXIT_CANNOT_RUN, message, hint)


def waiting(message: str, hint: str = "") -> Stop:
    return Stop(EXIT_WAITING, message, hint)


KIND = {EXIT_REFUSED: "refused", EXIT_CANNOT_RUN: "cannot-run", EXIT_WAITING: "waiting"}


def say(text: str) -> None:
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Dirs:
    """Where rbox keeps things. Fixed under the real home so the environment
    cannot point rbox at someone else's approvals. RBOX_TEST_BASE moves the
    cache and state into a separate test world; the toolchain stays shared."""

    def __init__(self, env: dict) -> None:
        self.home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        self.share = self.home / ".local" / "share" / "rustbox"
        base = env.get("RBOX_TEST_BASE")
        self.test_world = bool(base)
        if base:
            self.cache = Path(base).resolve() / "cache"
            self.state = Path(base).resolve() / "state"
        else:
            self.cache = self.home / ".cache" / "rustbox"
            self.state = self.home / ".local" / "state" / "rustbox"
        self.confirmed = self.state / "confirmed"
        self.urgent = self.state / "urgent"

    def toolchain(self) -> Path:
        try:
            version = (self.share / "toolchain").read_text(encoding="utf-8").strip()
        except OSError:
            raise cannot_run(
                "no Rust toolchain is installed for rbox",
                "run rustbox/install.sh from the kit checkout",
            )
        path = self.share / "toolchains" / version
        if not (path / "bin" / "cargo").is_file():
            raise cannot_run(f"toolchain {version} is missing from {path}", "run rustbox/install.sh again")
        return path


def find_bwrap() -> str:
    for place in BWRAP_PLACES:
        if os.path.isfile(place) and os.access(place, os.X_OK):
            return place
    raise cannot_run("bubblewrap (bwrap) is not installed", "a person runs: sudo apt install bubblewrap")


# ------------------------------------------------------------- human only


def _proc_environ_markers(pid: int) -> list:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            entries = f.read().split(b"\0")
    except OSError:
        return []
    found = []
    for marker in CLAUDE_MARKERS:
        prefix = marker.encode() + b"="
        if any(e.startswith(prefix) for e in entries):
            found.append(marker)
    return found


def _parent_pid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read()
        return int(data.rsplit(b")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def human_problems(env: dict, stdin_tty: bool, stdout_tty: bool, has_tty: bool, parent_markers: list) -> list:
    """Why this does not look like a person at their own terminal. Empty = ok."""
    problems = []
    if not (stdin_tty and stdout_tty and has_tty):
        problems.append("no real terminal")
    own = [m for m in CLAUDE_MARKERS if env.get(m)]
    if own:
        problems.append("this shell belongs to a Claude session (" + own[0] + ")")
    elif parent_markers:
        problems.append("a parent process is a Claude session (" + parent_markers[0] + ")")
    return problems


def require_human(what: str) -> None:
    markers: list = []
    pid = os.getpid()
    for _ in range(64):
        pid = _parent_pid(pid)
        if pid <= 1:
            break
        markers = _proc_environ_markers(pid)
        if markers:
            break
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        os.close(fd)
        has_tty = True
    except OSError:
        has_tty = False
    problems = human_problems(dict(os.environ), sys.stdin.isatty(), sys.stdout.isatty(), has_tty, markers)
    if problems:
        raise refuse(
            f"`rbox {what}` has to be typed by a person in their own terminal: " + "; ".join(problems),
            "open a separate terminal pane (not `!` inside Claude) and run it there",
        )


def ask(prompt: str) -> str:
    """Read one line from the controlling terminal, never from stdin."""
    with open("/dev/tty", "r+", encoding="utf-8", errors="replace") as tty:
        tty.write(prompt)
        tty.flush()
        return tty.readline().strip()


# ----------------------------------------------------------------- the box


def data_fd(content: bytes) -> int:
    """An inheritable in-memory file holding `content`, for bwrap --args / --ro-bind-data."""
    fd = os.memfd_create("rbox")
    os.write(fd, content)
    os.lseek(fd, 0, os.SEEK_SET)
    os.set_inheritable(fd, True)
    return fd


class Box:
    """Collects bwrap options and a complete environment, then runs a command.

    Nothing is inherited from the caller: the environment is built here and
    handed to bwrap directly (--clearenv alone still leaves the caller's
    variables readable through /proc/1/environ inside the box).
    """

    def __init__(self, bwrap: str) -> None:
        self.bwrap = bwrap
        self.opts: list = [
            "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent", "--new-session",
            "--tmpfs", "/tmp", "--proc", "/proc", "--dev", "/dev",
        ]
        self.env: dict = {}
        self.fds: list = []
        self.cwd = "/"

    def add(self, *opts: str) -> None:
        self.opts.extend(str(o) for o in opts)

    def data(self, dest: str, content: str) -> None:
        fd = data_fd(content.encode())
        self.fds.append(fd)
        self.add("--ro-bind-data", str(fd), dest)

    def system(self, full: bool) -> None:
        """Bind the host's system files read-only. `full` adds programs (/usr/bin);
        without it only libraries are visible, so nothing but the toolchain can run."""
        if full:
            self.add("--ro-bind", "/usr", "/usr")
        else:
            for name in ("lib", "lib32", "lib64", "libx32"):
                path = "/usr/" + name
                if os.path.islink(path):
                    self.add("--symlink", os.readlink(path), path)
                elif os.path.isdir(path):
                    self.add("--ro-bind", path, path)
        for name in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
            if not full and name in ("bin", "sbin"):
                continue
            path = "/" + name
            if os.path.islink(path):
                self.add("--symlink", os.readlink(path), path)
            elif os.path.isdir(path):
                self.add("--ro-bind", path, path)
        for path in ("/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/localtime"):
            self.add("--ro-bind-try", path, path)
        if full:
            self.add("--ro-bind-try", "/etc/alternatives", "/etc/alternatives")
        uid, gid = os.getuid(), os.getgid()
        self.data("/etc/passwd", f"rbox:x:{uid}:{gid}:rbox:/rbox/home:/bin/sh\n")
        self.data("/etc/group", f"rbox:x:{gid}:\n")

    def host_network(self) -> None:
        """Share the machine's network. Only ever used when nothing but cargo runs."""
        self.add("--share-net")
        self.add("--ro-bind-try", "/etc/ssl/certs", "/etc/ssl/certs")
        self.add("--ro-bind-try", "/usr/share/ca-certificates", "/usr/share/ca-certificates")
        try:
            resolv = Path("/etc/resolv.conf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            resolv = ""
        self.data("/etc/resolv.conf", resolv)
        self.data("/etc/hosts", "127.0.0.1 localhost\n::1 localhost\n")
        self.data("/etc/nsswitch.conf", "hosts: files dns\n")

    def toolchain(self, path: Path) -> None:
        self.add("--dir", "/rbox", "--tmpfs", "/rbox/home", "--dir", "/rbox/home/cargo")
        self.add("--ro-bind", str(path), "/rbox/toolchain")
        self.env.update({
            "HOME": "/rbox/home",
            "CARGO_HOME": "/rbox/home/cargo",
            "LANG": "C.UTF-8",
            "TERM": os.environ.get("TERM") or "dumb",
        })

    def argv(self, command: list) -> list:
        packed = b"".join(o.encode() + b"\0" for o in [*self.opts, "--chdir", self.cwd])
        fd = data_fd(packed)
        self.fds.append(fd)
        return [self.bwrap, "--args", str(fd), "--", *command]

    def exec(self, command: list) -> None:
        """Replace this process with the boxed command (nothing of rbox stays in between)."""
        argv = self.argv(command)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(self.bwrap, argv, self.env)

    def run(self, command: list, capture: bool = False, stdin=None) -> subprocess.CompletedProcess:
        argv = self.argv(command)
        try:
            return subprocess.run(
                argv, env=self.env, pass_fds=self.fds, text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
                stdin=stdin,
            )
        finally:
            for fd in self.fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            self.fds = []


# ------------------------------------------------------------- the project


class Project:
    def __init__(self, dirs: Dirs, root: Path) -> None:
        self.root = root
        self.key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        self.cache = dirs.cache / self.key
        self.state = dirs.state / self.key
        self.vendor = self.cache / "vendor"
        self.target = self.cache / "target"
        self.lock = root / "Cargo.lock"

    def prepare(self) -> None:
        for path in (self.vendor, self.target, self.state):
            path.mkdir(parents=True, exist_ok=True)
        origin = self.cache / "origin"
        if not origin.exists():
            write_atomic(origin, str(self.root) + "\n")


def check_root(dirs: Dirs, root: Path) -> None:
    """Refuse project roots that would expose more than a project."""
    if os.getuid() == 0:
        raise refuse("rbox does not run as root")
    if root == Path("/") or root == dirs.home or root in dirs.home.parents:
        raise refuse(f"{root} is not a project folder (it is your home, or above it)")
    for own in (dirs.share, dirs.cache, dirs.state):
        if root == own or own in root.parents:
            raise refuse(f"{root} is inside rbox's own folders")


def hidden_paths(root: Path) -> tuple:
    """(dirs, files) to hide in every box: .git, .claude, .mcp.json, .env, .env.*
    wherever they appear in the project tree."""
    hide_dirs: list = []
    hide_files: list = []
    seen = 0
    for current, subdirs, files in os.walk(root):
        seen += len(subdirs) + len(files)
        if seen > WALK_LIMIT:
            say(f"rbox: note: {root} is very large; secrets deeper than the first {WALK_LIMIT} entries are not hidden")
            break
        keep = []
        for name in subdirs:
            path = os.path.join(current, name)
            if name in HIDE_DIRS:
                if not os.path.islink(path):
                    hide_dirs.append(path)
                continue
            if name in WALK_PRUNE or os.path.islink(path):
                continue
            keep.append(name)
        subdirs[:] = keep
        for name in files:
            if name in HIDE_FILES or name.startswith(".env."):
                path = os.path.join(current, name)
                real = os.path.realpath(path)
                if os.path.isfile(real) and (real == str(root) or real.startswith(str(root) + os.sep)):
                    hide_files.append(real)
    return hide_dirs, sorted(set(hide_files))


def cargo_config_dirs(root: Path, cwd: Path) -> list:
    """Every .cargo between cwd and the root, to hide in the fetch box. Cargo reads
    both `.cargo/config` and `.cargo/config.toml`, at each level, walking up from cwd."""
    found = []
    here = cwd
    while True:
        candidate = here / ".cargo"
        if candidate.is_symlink():
            raise refuse(f"{candidate} is a symlink; rbox will not follow it")
        if candidate.exists():
            found.append(candidate)
        if here == root or root not in here.parents:
            break
        here = here.parent
    return found


def hide_cargo_config(box: "Box", root: Path, cwd: Path) -> None:
    for cfg in cargo_config_dirs(root, cwd):
        if cfg.is_dir():
            box.add("--tmpfs", str(cfg))
        else:
            box.add("--ro-bind", "/dev/null", str(cfg))


def hide_secrets(box: "Box", root: Path) -> None:
    hide_dirs, hide_files = hidden_paths(root)
    for path in hide_dirs:
        box.add("--tmpfs", path)
    for path in hide_files:
        box.add("--ro-bind", "/dev/null", path)


def discovery_scope(dirs: Dirs, start: Path) -> Path:
    """The topmost folder at or above `start` that holds a Cargo.toml and is safe to
    show to cargo, so cargo's own walk for the workspace root sees every manifest."""
    best = None
    here = start
    while True:
        if (here / "Cargo.toml").is_file():
            try:
                check_root(dirs, here)
                best = here
            except Stop:
                break
        if here.parent == here:
            break
        here = here.parent
    if best is None:
        raise refuse(f"no Cargo.toml at or above {start}", "run this inside a Rust project, or create one with `rbox new NAME`")
    return best


def find_project(dirs: Dirs, bwrap: str, toolchain: Path, cwd: Path, manifest: Path | None) -> Project:
    """The workspace root as cargo itself reports it for cwd (or for --manifest-path)."""
    start = manifest.parent if manifest else cwd
    scope = discovery_scope(dirs, start)
    box = Box(bwrap)
    box.system(full=False)
    box.toolchain(toolchain)
    box.env["PATH"] = "/rbox/toolchain/bin"
    box.env.update({"RUSTC": "/rbox/toolchain/bin/rustc", "RUSTC_WRAPPER": "", "RUSTC_WORKSPACE_WRAPPER": ""})
    box.add("--ro-bind", str(scope), str(scope))
    hide_cargo_config(box, scope, start)
    box.cwd = str(start)
    command = ["/rbox/toolchain/bin/cargo", "locate-project", "--workspace", "--message-format", "plain"]
    if manifest:
        command += ["--manifest-path", str(manifest)]
    done = box.run(command, capture=True)
    if done.returncode != 0:
        raise refuse("cargo could not work out the workspace root:\n" + done.stderr.strip())
    located = Path(done.stdout.strip())
    if located.name != "Cargo.toml" or not located.is_file():
        raise refuse(f"cargo reported an unexpected workspace root: {done.stdout.strip()!r}")
    root = located.parent
    if root != scope and scope not in root.parents:
        raise refuse(f"workspace root {root} is outside {scope}")
    check_root(dirs, root)
    return Project(dirs, root)


# ---------------------------------------------------------------- the boxes


def passthrough_env(env: dict, names: tuple) -> dict:
    return {n: env[n] for n in names if n in env}


def color_env() -> dict:
    if os.environ.get("NO_COLOR") or not sys.stderr.isatty():
        return {"CARGO_TERM_COLOR": os.environ.get("CARGO_TERM_COLOR", "never")}
    return {"CARGO_TERM_COLOR": os.environ.get("CARGO_TERM_COLOR", "always")}


SOURCE_PINS = [
    "--config", 'source.crates-io.replace-with="rbox-vendor"',
    "--config", 'source.rbox-vendor.directory="/rbox/vendor"',
    "--config", "net.offline=true",
]
SOURCE_CONFIG = (
    '[source.crates-io]\nreplace-with = "rbox-vendor"\n\n'
    '[source.rbox-vendor]\ndirectory = "/rbox/vendor"\n\n[net]\noffline = true\n'
)


def cooldown_pins(mode: str = "deny") -> list:
    age = f"{COOLDOWN_DAYS} days"
    return [
        "--config", f'registry.global-min-publish-age="{age}"',
        "--config", f'registries.crates-io.min-publish-age="{age}"',
        "--config", f'resolver.incompatible-publish-age="{mode}"',
    ]


def build_box(dirs: Dirs, bwrap: str, toolchain: Path, project: Project, cwd: Path, extra_env: dict | None = None) -> Box:
    """Crate code may run here: no network, project read-only, vendored crates only."""
    project.prepare()
    box = Box(bwrap)
    box.system(full=True)
    box.toolchain(toolchain)
    box.data("/rbox/home/cargo/config.toml", SOURCE_CONFIG)
    box.add("--ro-bind", str(project.vendor), "/rbox/vendor")
    box.add("--bind", str(project.target), "/rbox/target")
    box.add("--ro-bind", str(project.root), str(project.root))
    hide_secrets(box, project.root)
    box.env.update({
        "PATH": "/rbox/toolchain/bin:/usr/bin:/bin",
        "CARGO_TARGET_DIR": "/rbox/target",
        "CARGO_BUILD_BUILD_DIR": "/rbox/target",
        "CARGO_NET_OFFLINE": "true",
    })
    box.env.update(color_env())
    box.env.update(passthrough_env(dict(os.environ), PASS_ENV))
    if extra_env:
        box.env.update(extra_env)
    box.cwd = str(cwd)
    return box


def fetch_box(dirs: Dirs, bwrap: str, toolchain: Path, root: Path, cwd: Path, network: bool,
              vendor: Path | None = None, cooldown: str = "deny") -> Box:
    """Only cargo and rustfmt run here: no programs from the host, project cargo
    config hidden, cooldown forced. The project is writable except what is hidden."""
    cargo_home = dirs.cache / "cargo-home"
    cargo_home.mkdir(parents=True, exist_ok=True)
    box = Box(bwrap)
    box.system(full=False)
    box.toolchain(toolchain)
    box.add("--bind", str(cargo_home), "/rbox/home/cargo")
    box.data("/rbox/home/cargo/config.toml", "")
    if vendor is not None:
        vendor.mkdir(parents=True, exist_ok=True)
        box.add("--bind", str(vendor), "/rbox/vendor")
    box.add("--bind", str(root), str(root))
    hide_secrets(box, root)
    hide_cargo_config(box, root, cwd)
    age = f"{COOLDOWN_DAYS} days"
    box.env.update({
        "PATH": "/rbox/toolchain/bin",
        "RUSTC": "/rbox/toolchain/bin/rustc",
        "RUSTC_WRAPPER": "",
        "RUSTC_WORKSPACE_WRAPPER": "",
        "CARGO_NET_GIT_FETCH_WITH_CLI": "false",
        "CARGO_REGISTRY_GLOBAL_MIN_PUBLISH_AGE": age,
        "CARGO_REGISTRIES_CRATES_IO_MIN_PUBLISH_AGE": age,
        "CARGO_RESOLVER_INCOMPATIBLE_PUBLISH_AGE": cooldown,
        "CARGO_TARGET_DIR": "/tmp/rbox-fetch-target",
    })
    box.env.update(color_env())
    if network:
        box.host_network()
        box.env.update(passthrough_env(dict(os.environ), PROXY_ENV))
        if os.path.isfile("/etc/ssl/certs/ca-certificates.crt"):
            box.env["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"
    else:
        box.env["CARGO_NET_OFFLINE"] = "true"
    box.cwd = str(cwd)
    return box


CARGO = "/rbox/toolchain/bin/cargo"


# ---------------------------------------------------------------- toolchain


def machine_triple() -> str:
    machine = os.uname().machine
    if machine in ("x86_64", "amd64"):
        return "x86_64-unknown-linux-gnu"
    if machine in ("aarch64", "arm64"):
        return "aarch64-unknown-linux-gnu"
    raise cannot_run(f"there is no pinned toolchain for machine type {machine}")


def read_sums(path: Path, triple: str) -> tuple:
    """(version, [(sha256, url), ...]) for this machine from toolchain.sums."""
    version = ""
    parts = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise cannot_run(f"cannot read the toolchain pin {path}: {exc}")
    for line in lines:
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0] == "version" and len(fields) == 2:
            version = fields[1]
        elif fields[0] == triple and len(fields) == 3:
            sha, url = fields[1], fields[2]
            if len(sha) != 64 or not url.startswith("https://static.rust-lang.org/dist/"):
                raise cannot_run(f"bad line in {path}: {line}")
            parts.append((sha, url))
    if not version or not parts:
        raise cannot_run(f"{path} has no toolchain pin for {triple}")
    return version, parts


def download(url: str, dest: Path, sha: str) -> None:
    if dest.is_file() and sha256_file(dest) == sha:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    say(f"  downloading {dest.name}")
    request = urllib.request.Request(url, headers={"User-Agent": "rbox"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
    except (urllib.error.URLError, OSError) as exc:
        raise cannot_run(f"could not download {url}: {exc}")
    got = sha256_file(part)
    if got != sha:
        part.unlink()
        raise cannot_run(f"hash mismatch for {dest.name}: expected {sha}, got {got}")
    os.replace(part, dest)


def cmd_toolchain(dirs: Dirs, args: list) -> int:
    if not args or args[0] != "install":
        raise refuse("usage: rbox toolchain install [PIN-FILE]")
    sums = Path(args[1]).resolve() if len(args) > 1 else dirs.share / "toolchain.sums"
    version, parts = read_sums(sums, machine_triple())
    final = dirs.share / "toolchains" / version
    if (final / ".rbox-complete").is_file() and (final / "bin" / "cargo").is_file():
        write_atomic(dirs.share / "toolchain", version + "\n")
        say(f"rbox: Rust {version} is already installed")
        return 0
    bwrap = find_bwrap()
    dl = dirs.home / ".cache" / "rustbox" / "dl"
    names = []
    for sha, url in parts:
        name = url.rsplit("/", 1)[1]
        download(url, dl / name, sha)
        names.append(name)
    work = dl / f"extract-{os.getpid()}"
    dest = final.parent / f".{version}.tmp-{os.getpid()}"
    work.mkdir(parents=True)
    dest.mkdir(parents=True)
    say(f"  installing Rust {version} (inside a box, no network)")
    box = Box(bwrap)
    box.system(full=True)
    box.add("--dir", "/rbox", "--ro-bind", str(dl), "/rbox/dl", "--bind", str(work), "/rbox/work", "--bind", str(dest), "/rbox/dest")
    box.env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"}
    box.cwd = "/rbox/work"
    script = (
        'set -e\nfor t in "$@"; do\n  tar -xJf "/rbox/dl/$t"\n  d="${t%.tar.xz}"\n'
        '  sh "$d/install.sh" --prefix=/rbox/dest --disable-ldconfig >/dev/null\n  rm -rf "$d"\ndone\n'
    )
    try:
        done = box.run(["/bin/sh", "-c", script, "sh", *names], capture=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if done.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise cannot_run("the toolchain installer failed:\n" + (done.stderr or done.stdout).strip()[-800:])
    (dest / ".rbox-complete").write_text(version + "\n", encoding="utf-8")
    if final.exists():
        shutil.rmtree(final)
    os.rename(dest, final)
    write_atomic(dirs.share / "toolchain", version + "\n")
    say(f"rbox: installed Rust {version} in {final}")
    return 0


# -------------------------------------------------------------- crates.io


def index_path(name: str) -> str:
    n = name.lower()
    if len(n) == 1:
        return f"1/{n}"
    if len(n) == 2:
        return f"2/{n}"
    if len(n) == 3:
        return f"3/{n[0]}/{n}"
    return f"{n[:2]}/{n[2:4]}/{n}"


def fetch_index(name: str) -> dict:
    """{version: {"pubtime", "cksum", "yanked"}} for one crate, or {"error": ...}."""
    request = urllib.request.Request(INDEX_URL + index_path(name), headers={"User-Agent": "rbox"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return {} if exc.code in (404, 410) else {"error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"error": str(getattr(exc, "reason", exc))}
    versions = {}
    for line in body.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "vers" in row:
            versions[str(row["vers"])] = {
                "pubtime": row.get("pubtime"), "cksum": row.get("cksum"), "yanked": bool(row.get("yanked")),
            }
    return versions


def lookup_index(pairs: list) -> dict:
    """{"name version": info}. info has `missing` when crates.io does not list that
    version, or `unknown` when the index could not be reached."""
    names = sorted({name for name, _ in pairs})
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for name, versions in zip(names, pool.map(fetch_index, names)):
            results[name] = versions
    out = {}
    for name, version in pairs:
        versions = results.get(name, {"error": "not fetched"})
        if "error" in versions:
            out[f"{name} {version}"] = {"unknown": versions["error"]}
        elif version not in versions:
            out[f"{name} {version}"] = {"missing": True}
        else:
            out[f"{name} {version}"] = versions[version]
    return out


def age_days(pubtime: str | None) -> float | None:
    if not pubtime:
        return None
    try:
        when = datetime.datetime.fromisoformat(pubtime.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - when).total_seconds() / 86400


def age_text(days: float | None) -> str:
    if days is None:
        return "publish date unknown"
    if days < 2:
        return f"published {max(1, int(days * 24))} hours ago"
    if days < 60:
        return f"published {int(days)} days ago"
    if days < 730:
        return f"published {int(days / 30.4)} months ago"
    return f"published {int(days / 365.25)} years ago"


# -------------------------------------------------------------- approvals


def load_confirmed(dirs: Dirs) -> dict:
    """{(name, version, sha256): date} — the machine-wide set of crate files a person approved."""
    confirmed = {}
    try:
        lines = dirs.confirmed.read_text(encoding="utf-8").splitlines()
    except OSError:
        return confirmed
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and not fields[0].startswith("#"):
            confirmed[(fields[0], fields[1], fields[2])] = fields[3] if len(fields) > 3 else ""
    return confirmed


def add_confirmed(dirs: Dirs, entries: list) -> None:
    try:
        text = dirs.confirmed.read_text(encoding="utf-8")
    except OSError:
        text = "# Crate files approved on this machine by a person, via `rbox allow`.\n# name version sha256 date\n"
    today = datetime.date.today().isoformat()
    have = load_confirmed(dirs)
    for name, version, checksum in entries:
        if (name, version, checksum) not in have:
            text += f"{name} {version} {checksum} {today}\n"
    write_atomic(dirs.confirmed, text)


def load_pairs(path: Path) -> set:
    found = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and not fields[0].startswith("#"):
                found.add((fields[0], fields[1]))
    except OSError:
        pass
    return found


def add_pairs(path: Path, pairs: list, header: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = header
    have = load_pairs(path)
    today = datetime.date.today().isoformat()
    for a, b in pairs:
        if (a, b) not in have:
            text += f"{a} {b} {today}\n"
    write_atomic(path, text)


def vendor_checksum(project: Project, name: str, version: str) -> str:
    info = read_json(project.vendor / f"{name}-{version}" / ".cargo-checksum.json", {})
    value = info.get("package") if isinstance(info, dict) else None
    return value if isinstance(value, str) else ""


def collect_meta(dirs: Dirs, bwrap: str, toolchain: Path, project: Project) -> list:
    """Ask cargo itself what the lockfile resolves to. No lockfile parsing here."""
    box = build_box(dirs, bwrap, toolchain, project, project.root)
    done = box.run([CARGO, "metadata", "--format-version", "1", "--locked", *SOURCE_PINS], capture=True)
    if done.returncode != 0:
        raise refuse("cargo could not read the dependency set:\n" + done.stderr.strip()[-1200:])
    try:
        meta = json.loads(done.stdout)
    except ValueError as exc:
        raise refuse(f"cargo metadata returned something unreadable: {exc}")
    members = set(meta["workspace_members"])
    packages = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in (meta.get("resolve") or {}).get("nodes", [])}
    via: dict = {}
    for member in sorted(members):
        for dep in nodes.get(member, {}).get("deps", []):
            top = packages[dep["pkg"]]["name"]
            stack = [dep["pkg"]]
            while stack:
                current = stack.pop()
                if current in via or current in members:
                    continue
                via[current] = top
                stack.extend(d["pkg"] for d in nodes.get(current, {}).get("deps", []))
    patched = set()
    for node in nodes.values():
        if packages[node["id"]]["source"] is not None:
            for dep in node["deps"]:
                if packages[dep["pkg"]]["source"] is None:
                    patched.add(dep["pkg"])
    rows = []
    for pid, package in sorted(packages.items(), key=lambda item: (item[1]["name"], item[1]["version"])):
        local = package["source"] is None
        if pid in members and pid not in patched:
            continue
        kinds = {kind for target in package["targets"] for kind in target["kind"]}
        row = {
            "name": package["name"], "version": package["version"], "source": package["source"],
            "build_script": "custom-build" in kinds, "proc_macro": "proc-macro" in kinds,
            "native": bool(package.get("links")),
            "via": "" if via.get(pid) == package["name"] else via.get(pid, ""),
        }
        if local:
            folder = Path(package["manifest_path"]).parent
            try:
                row["path"] = str(folder.relative_to(project.root)) or "."
            except ValueError:
                row["path"] = str(folder)
            row["patched"] = pid in patched
        else:
            row["checksum"] = vendor_checksum(project, package["name"], package["version"])
        rows.append(row)
    return rows


def approval_state(dirs: Dirs, project: Project) -> dict:
    """What stands between this project and a build. Raises Stop(3) when the crates
    are not downloaded for the current lockfile."""
    if not project.lock.is_file():
        raise refuse("there is no Cargo.lock yet", "run `rbox sync` (it resolves and downloads, nothing runs)")
    lock_hash = sha256_file(project.lock)
    meta = read_json(project.state / "meta.json", {})
    if not isinstance(meta, dict) or meta.get("lock") != lock_hash:
        raise refuse(
            "the dependencies changed since the crates were last downloaded",
            "run `rbox sync` (it only downloads, nothing runs)",
        )
    rows = meta.get("rows", [])
    confirmed = load_confirmed(dirs)
    local_ok = load_pairs(project.state / "local-ok")
    registry = [r for r in rows if r.get("source") is not None]
    local = [r for r in rows if r.get("source") is None]
    return {
        "rows": rows,
        "bad_source": [r for r in registry if r["source"] != CRATES_IO],
        "broken": [r for r in registry if r["source"] == CRATES_IO and not r.get("checksum")],
        "pending": [
            r for r in registry
            if r["source"] == CRATES_IO and r.get("checksum") and (r["name"], r["version"], r["checksum"]) not in confirmed
        ],
        "local_pending": [r for r in local if (r["name"], r.get("path", "")) not in local_ok],
        "confirmed": confirmed,
    }


def require_approved(dirs: Dirs, project: Project) -> None:
    state = approval_state(dirs, project)
    if state["bad_source"]:
        names = ", ".join(f"{r['name']} ({r['source'].split('+')[0]})" for r in state["bad_source"][:6])
        raise refuse(f"crates from outside crates.io: {names}", "rbox v1 builds crates.io crates only")
    if state["broken"]:
        names = ", ".join(f"{r['name']} {r['version']}" for r in state["broken"][:6])
        raise refuse(f"the downloaded copy is incomplete for: {names}", "run `rbox sync`")
    waiting_for = [f"{r['name']} {r['version']}" for r in state["pending"]]
    waiting_for += [f"{r['name']} (local: {r.get('path', '?')})" for r in state["local_pending"]]
    if waiting_for:
        shown = ", ".join(waiting_for[:8]) + (f" (+{len(waiting_for) - 8} more)" if len(waiting_for) > 8 else "")
        noun = "crate awaits" if len(waiting_for) == 1 else "crates await"
        raise waiting(
            f"{len(waiting_for)} {noun} approval: {shown}",
            "a person runs `rbox allow` in their own terminal; `rbox allow --show` lists them",
        )


# -------------------------------------------------------------------- sync


def run_fetch(dirs: Dirs, bwrap: str, toolchain: Path, project: Project, cwd: Path, command: list,
              network: bool, vendor: bool = False, cooldown: str = "deny", quiet: bool = False) -> int:
    box = fetch_box(dirs, bwrap, toolchain, project.root, cwd, network,
                    vendor=project.vendor if vendor else None, cooldown=cooldown)
    done = box.run(command, capture=quiet)
    if quiet and done.returncode != 0:
        sys.stderr.write(done.stderr)
    return done.returncode


def do_sync(dirs: Dirs, bwrap: str, toolchain: Path, project: Project, quiet: bool = False) -> int:
    """Make the downloaded copy match Cargo.lock, then record what cargo resolved.
    Nothing from the crates runs: only cargo, in the fetch box."""
    project.prepare()
    # 1. resolve (creates or minimally updates Cargo.lock under the cooldown) and download
    code = run_fetch(dirs, bwrap, toolchain, project, project.root,
                     [CARGO, "fetch", *cooldown_pins()], network=True, quiet=quiet)
    if code != 0:
        return code
    # 2. unpack into the per-project vendored copy
    code = run_fetch(dirs, bwrap, toolchain, project, project.root,
                     [CARGO, "vendor", "--locked", "--versioned-dirs", "/rbox/vendor"],
                     network=True, vendor=True, quiet=True)
    if code != 0:
        return code
    # 3. what did cargo resolve, and what does crates.io say about each file
    rows = collect_meta(dirs, bwrap, toolchain, project)
    registry = [(r["name"], r["version"]) for r in rows if r.get("source") == CRATES_IO]
    index = read_json(project.state / "index.json", {})
    if not isinstance(index, dict):
        index = {}
    need = [pair for pair in registry if "pubtime" not in index.get(f"{pair[0]} {pair[1]}", {})]
    if need:
        index.update(lookup_index(need))
    index = {key: index[key] for key in (f"{n} {v}" for n, v in registry) if key in index}
    mismatched = []
    for row in rows:
        info = index.get(f"{row['name']} {row['version']}", {})
        if row.get("checksum") and info.get("cksum") and info["cksum"] != row["checksum"]:
            mismatched.append(f"{row['name']} {row['version']}")
    write_atomic(project.state / "index.json", json.dumps(index, indent=0, sort_keys=True))
    if mismatched:
        raise refuse(
            "the downloaded file does not match the crates.io index for: " + ", ".join(mismatched),
            "do not build; this should never happen with an honest download",
        )
    write_atomic(project.state / "meta.json", json.dumps({"lock": sha256_file(project.lock), "rows": rows}))
    unknown = sum(1 for key in index if "unknown" in index[key])
    if not quiet:
        say(f"rbox: synced {len(registry)} crates for {project.root.name}"
            + (f" ({unknown} publish dates could not be fetched)" if unknown else ""))
    return 0


# ---------------------------------------------------------------- cargo


def split_cargo(args: list) -> tuple:
    """(leading flags, subcommand or None, the rest). Refuses what rbox does not allow
    before the subcommand."""
    leading = []
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("+"):
            raise refuse(f"`{token}`: rbox pins one toolchain; toolchain overrides are not supported")
        if token in LEADING_OK:
            leading.append(token)
        elif token == "--color" and i + 1 < len(args):
            leading += [token, args[i + 1]]
            i += 1
        elif token.startswith("--color="):
            leading.append(token)
        elif token.startswith("-") and token not in NO_PROJECT_CMDS:
            if token == "--explain" and i + 1 < len(args):
                return leading, "--explain", args[i:]
            raise refuse(f"`cargo {token}` is not available through rbox")
        else:
            return leading, token, args[i + 1:]
        i += 1
    return leading, None, []


def before_dashdash(rest: list) -> list:
    return rest[: rest.index("--")] if "--" in rest else rest


def check_flags(sub: str, leading: list, rest: list) -> None:
    for token in leading + before_dashdash(rest):
        for flag in REFUSED_FLAGS:
            if token == flag or token.startswith(flag + "=") or (flag == "-Z" and token.startswith("-Z")):
                raise refuse(f"`{flag}` would change rbox's rules for this command")
    head = before_dashdash(rest)
    if sub == "clippy" and "--fix" in head:
        raise refuse("`clippy --fix` compiles crate code and rewrites your sources in one step")
    if sub in ("doc", "d") and "--open" in head:
        raise refuse("there is no browser inside the box; drop `--open`")
    if sub == "add" and any(t in ("--git", "--registry") or t.startswith(("--git=", "--registry=")) for t in head):
        raise refuse("only crates.io crates can be added in v1 (no --git, no --registry)")


def manifest_arg(rest: list, cwd: Path) -> Path | None:
    head = before_dashdash(rest)
    for i, token in enumerate(head):
        value = None
        if token == "--manifest-path" and i + 1 < len(head):
            value = head[i + 1]
        elif token.startswith("--manifest-path="):
            value = token.split("=", 1)[1]
        if value is not None:
            path = (cwd / value).resolve()
            if path.name != "Cargo.toml" or not path.is_file():
                raise refuse(f"--manifest-path {value}: not a Cargo.toml")
            return path
    return None


def strip_lock_flags(tokens: list) -> list:
    head = before_dashdash(tokens)
    tail = tokens[len(head):]
    return [t for t in head if t not in ("--locked", "--offline", "--frozen")] + tail


INFO_CMDS = {"metadata", "tree", "pkgid", "locate-project"}  # read the dependency set, compile nothing


def cmd_cargo(dirs: Dirs, args: list, net: bool, extra_env: dict) -> int:
    leading, sub, rest = split_cargo(args)
    bwrap = find_bwrap()
    toolchain = dirs.toolchain()
    cwd = Path.cwd().resolve()
    if sub is None or sub in NO_PROJECT_CMDS or sub == "--explain":
        box = Box(bwrap)
        box.system(full=False)
        box.toolchain(toolchain)
        box.env["PATH"] = "/rbox/toolchain/bin"
        box.env.update(color_env())
        box.exec([CARGO, *leading, *([sub] if sub else []), *rest] if sub != "--explain" else [CARGO, *leading, *rest])
    if sub in REFUSED_CMDS:
        raise refuse(f"`cargo {sub}`: {REFUSED_CMDS[sub]}")
    known = BUILD_CMDS | FETCH_NET_CMDS | SYNC_CMDS | FETCH_LOCAL_CMDS
    if sub not in known:
        raise refuse(f"`cargo {sub}` is not a command rbox knows", "rbox runs cargo's own commands only, no plugins")
    check_flags(sub, leading, rest)
    if net and sub not in NET_OK_CMDS:
        raise refuse(f"`--net` applies to test, run and bench only, not to `cargo {sub}`")
    leading = strip_lock_flags(leading)
    rest = strip_lock_flags(rest)
    if sub == "search":
        box = fetch_box(dirs, bwrap, toolchain, _empty_dir(dirs), _empty_dir(dirs), network=True)
        return box.run([CARGO, *leading, sub, *rest]).returncode
    project = find_project(dirs, bwrap, toolchain, cwd, manifest_arg(rest, cwd))
    if cwd != project.root and project.root not in cwd.parents:
        cwd = project.root
    if sub in SYNC_CMDS:
        return do_sync(dirs, bwrap, toolchain, project)
    if sub in FETCH_LOCAL_CMDS:
        return run_fetch(dirs, bwrap, toolchain, project, cwd, [CARGO, *leading, sub, *rest], network=False)
    if sub in FETCH_NET_CMDS:
        code = run_fetch(dirs, bwrap, toolchain, project, cwd,
                         [CARGO, *leading, sub, *cooldown_pins(), *rest], network=True)
        if code != 0:
            return code
        return do_sync(dirs, bwrap, toolchain, project, quiet=True)
    # build box
    project.prepare()
    if sub != "locate-project":
        if sub in INFO_CMDS:
            approval_state(dirs, project)  # the crates must be downloaded; approval is not needed to look
        else:
            require_approved(dirs, project)
    if net:
        return run_with_network(dirs, bwrap, toolchain, project, cwd,
                                [CARGO, *leading, sub, "--locked", *SOURCE_PINS, *rest], extra_env)
    box = build_box(dirs, bwrap, toolchain, project, cwd, extra_env)
    box.exec([CARGO, *leading, sub, "--locked", *SOURCE_PINS, *rest])
    return 0


def _empty_dir(dirs: Dirs) -> Path:
    path = dirs.cache / "empty"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_with_network(dirs: Dirs, bwrap: str, toolchain: Path, project: Project, cwd: Path,
                     command: list, extra_env: dict, binds: list | None = None) -> int:
    raise refuse("allow-listed network (`--net`) is not built yet in this version of rbox")


# ------------------------------------------------------------------- allow


def describe_pending(dirs: Dirs, project: Project, state: dict) -> tuple:
    """(approvable, refused): every pending registry crate with what a person needs
    to judge it. Refused ones cannot be approved today, and say why."""
    index = read_json(project.state / "index.json", {})
    if not isinstance(index, dict):
        index = {}
    stale = [(r["name"], r["version"]) for r in state["pending"]
             if "pubtime" not in index.get(f"{r['name']} {r['version']}", {})
             and "missing" not in index.get(f"{r['name']} {r['version']}", {})]
    if stale:
        index.update(lookup_index(stale))
        write_atomic(project.state / "index.json", json.dumps(index, indent=0, sort_keys=True))
    urgent = load_pairs(dirs.urgent)
    latest: dict = {}
    for (name, version, _), date in state["confirmed"].items():
        if name not in latest or (date, version) > latest[name]:
            latest[name] = (date, version)
    approvable, refused = [], []
    for row in state["pending"]:
        info = index.get(f"{row['name']} {row['version']}", {})
        days = age_days(info.get("pubtime"))
        item = dict(row)
        item["days"] = days
        item["was"] = latest[row["name"]][1] if row["name"] in latest else ""
        item["runs_code"] = bool(row.get("build_script") or row.get("proc_macro"))
        item["yanked"] = bool(info.get("yanked"))
        item["urgent"] = (row["name"], row["version"]) in urgent
        if info.get("missing"):
            item["why"] = "not in the crates.io index (removed?)"
            refused.append(item)
        elif days is None:
            item["why"] = "publish date could not be checked (" + str(info.get("unknown", "no date")) + ")"
            refused.append(item)
        elif days < COOLDOWN_DAYS and not item["urgent"]:
            ready = datetime.date.today() + datetime.timedelta(days=int(COOLDOWN_DAYS - days) + 1)
            item["why"] = f"too new ({age_text(days)}); approvable from {ready.isoformat()}, or `rbox urgent {row['name']}@{row['version']}`"
            refused.append(item)
        else:
            item["by_name"] = item["urgent"] or (item["runs_code"] and days < YOUNG_DAYS)
            approvable.append(item)
    return approvable, refused


def crate_line(item: dict) -> list:
    change = f"{item['was']} -> {item['version']}" if item.get("was") else f"{item['version']} (new)"
    flags = [text for key, text in (("build_script", "build script"), ("proc_macro", "proc macro"), ("native", "native lib"))
             if item.get(key)]
    if item.get("yanked"):
        flags.append("YANKED")
    if item.get("urgent"):
        flags.append("urgent override")
    via = f"via {item['via']}" if item.get("via") else "direct"
    return [item["name"], change, age_text(item.get("days")), ", ".join(flags), via]


def print_table(title: str, lines: list, out) -> None:
    if not lines:
        return
    widths = [max(len(line[i]) for line in lines) for i in range(len(lines[0]))]
    out.write(title + "\n")
    for line in lines:
        out.write("  " + "   ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip() + "\n")


def show_pending(approvable: list, refused: list, local: list, out) -> None:
    code = [i for i in approvable if i["runs_code"]]
    other = [i for i in approvable if not i["runs_code"]]
    print_table(f"RUNS CODE AT BUILD TIME ({len(code)})", [crate_line(i) for i in code], out)
    print_table(f"OTHER ({len(other)})", [crate_line(i) for i in other], out)
    print_table(
        f"LOCAL CODE STANDING IN FOR A CRATE ({len(local)})",
        [[r["name"], "path: " + r.get("path", "?"),
          "replaces a crates.io crate ([patch])" if r.get("patched") else "path dependency outside the workspace"]
         for r in local], out)
    print_table(f"CANNOT BE APPROVED TODAY ({len(refused)})",
                [[i["name"], i["version"], i["why"]] for i in refused], out)
    out.flush()


def cmd_allow(dirs: Dirs, args: list) -> int:
    show = "--show" in args
    tsv = "--tsv" in args
    unknown = [a for a in args if a not in ("--show", "--tsv")]
    if unknown:
        raise refuse(f"`rbox allow` takes no crate names (got {unknown[0]}); it reviews everything pending")
    if not (show or tsv):
        require_human("allow")
    bwrap = find_bwrap()
    toolchain = dirs.toolchain()
    project = find_project(dirs, bwrap, toolchain, Path.cwd().resolve(), None)
    state = approval_state(dirs, project)
    if state["bad_source"]:
        names = ", ".join(r["name"] for r in state["bad_source"])
        raise refuse(f"crates from outside crates.io cannot be approved in v1: {names}")
    approvable, refused = describe_pending(dirs, project, state)
    local = state["local_pending"]
    if tsv:
        for item in approvable + refused:
            flags = ",".join(k for k in ("build_script", "proc_macro", "native") if item.get(k))
            days = "" if item.get("days") is None else f"{item['days']:.1f}"
            sys.stdout.write("\t".join([item["name"], item["version"], item["checksum"], flags, days,
                                        item.get("why", "approvable")]) + "\n")
        return 0
    if not (approvable or refused or local):
        say("rbox: nothing is waiting for approval")
        return 0
    if show:
        show_pending(approvable, refused, local, sys.stdout)
        return 0
    with open("/dev/tty", "w", encoding="utf-8") as tty:
        tty.write(f"\nProject: {project.root}\n\n")
        show_pending(approvable, refused, local, tty)
        tty.write("\n")
    approved, approved_local = [], []
    code = [i for i in approvable if i["runs_code"] and not i.get("by_name")]
    other = [i for i in approvable if not i["runs_code"] and not i.get("by_name")]
    named = [i for i in approvable if i.get("by_name")]
    for item in named:
        why = "urgent override" if item["urgent"] else f"runs code at build time and was {age_text(item['days'])}"
        typed = ask(f"{item['name']} {item['version']}: {why}.\n  Type its name to approve it, or Enter to skip: ")
        if typed == item["name"]:
            approved.append(item)
    if code and ask(f"Approve the {len(code)} crates that run code at build time? Type yes: ") == "yes":
        approved += code
    if other and ask(f"Approve the {len(other)} other crates? Type yes: ") == "yes":
        approved += other
    for row in local:
        if ask(f"Local code `{row['name']}` at {row.get('path', '?')} stands in for a crate. Approve? Type yes: ") == "yes":
            approved_local.append(row)
    if approved:
        add_confirmed(dirs, [(i["name"], i["version"], i["checksum"]) for i in approved])
    if approved_local:
        add_pairs(project.state / "local-ok", [(r["name"], r.get("path", "")) for r in approved_local],
                  "# Local crates approved for this project. name path date\n")
    left = len(approvable) - len(approved) + len(local) - len(approved_local) + len(refused)
    say(f"rbox: approved {len(approved) + len(approved_local)}, still waiting {left}")
    return 0 if left == 0 else EXIT_WAITING


def cmd_urgent(dirs: Dirs, args: list) -> int:
    if len(args) != 1 or "@" not in args[0]:
        raise refuse("usage: rbox urgent NAME@VERSION")
    require_human("urgent")
    name, version = args[0].split("@", 1)
    bwrap = find_bwrap()
    toolchain = dirs.toolchain()
    project = find_project(dirs, bwrap, toolchain, Path.cwd().resolve(), None)
    say(f"rbox: pulling {name} {version} past the {COOLDOWN_DAYS}-day cooldown (this one version only)")
    code = run_fetch(dirs, bwrap, toolchain, project, project.root,
                     [CARGO, "update", name, "--precise", version, *cooldown_pins("allow")],
                     network=True, cooldown="allow")
    if code != 0:
        return code
    code = do_sync(dirs, bwrap, toolchain, project, quiet=True)
    if code != 0:
        return code
    add_pairs(dirs.urgent, [(name, version)], "# Versions a person pulled past the cooldown. name version date\n")
    say("rbox: done. Now run `rbox allow`; it will ask you to type the crate's name.")
    return 0


# ------------------------------------------------------- the other commands


def open_project(dirs: Dirs) -> tuple:
    bwrap = find_bwrap()
    toolchain = dirs.toolchain()
    return bwrap, toolchain, find_project(dirs, bwrap, toolchain, Path.cwd().resolve(), None)


def cmd_sync(dirs: Dirs, args: list) -> int:
    bwrap, toolchain, project = open_project(dirs)
    return do_sync(dirs, bwrap, toolchain, project)


def dir_size(path: Path) -> int:
    total = 0
    for current, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                pass
    return total


def cmd_status(dirs: Dirs, args: list) -> int:
    bwrap, toolchain, project = open_project(dirs)
    out = sys.stdout
    out.write(f"project        {project.root}\n")
    out.write(f"toolchain      Rust {toolchain.name}\n")
    out.write(f"crate sources  {project.vendor}   (read these to look inside a dependency)\n")
    out.write(f"build output   {project.target}   ({dir_size(project.target) // (1 << 20)} MB; `rbox clean` empties it)\n")
    try:
        state = approval_state(dirs, project)
    except Stop as stop:
        out.write(f"dependencies   {stop.message}; {stop.hint}\n")
        return 0
    registry = [r for r in state["rows"] if r.get("source") is not None]
    out.write(f"dependencies   {len(registry)} crates locked, {len(state['pending'])} waiting for approval"
              + (" (`rbox allow --show`)" if state["pending"] else "") + "\n")
    local = [r for r in state["rows"] if r.get("source") is None]
    for row in local:
        note = "replaces a crates.io crate" if row.get("patched") else "path dependency"
        pending = "" if row not in state["local_pending"] else "  WAITING FOR APPROVAL"
        out.write(f"local crate    {row['name']} at {row.get('path', '?')} ({note}){pending}\n")
    hide_dirs, hide_files = hidden_paths(project.root)
    hidden = [os.path.relpath(p, project.root) for p in hide_dirs + hide_files]
    out.write("hidden in box  " + (", ".join(hidden[:12]) + (" ..." if len(hidden) > 12 else "") if hidden else "nothing") + "\n")
    return 0


def cmd_warm(dirs: Dirs, args: list) -> int:
    bwrap, toolchain, project = open_project(dirs)
    require_approved(dirs, project)
    for step in (["check", "--workspace", "--all-targets"], ["test", "--workspace", "--no-run"]):
        box = build_box(dirs, bwrap, toolchain, project, project.root)
        code = box.run([CARGO, step[0], "--locked", *SOURCE_PINS, *step[1:]]).returncode
        if code != 0:
            return code
    return 0


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def cmd_run(dirs: Dirs, args: list, net: bool, extra_env: dict) -> int:
    release = False
    writable: list = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            i += 1
            break
        if token == "--release":
            release = True
        elif token == "--net":
            net = True
        elif token == "--rw" and i + 1 < len(args):
            writable.append(args[i + 1])
            i += 1
        elif token == "--env" and i + 1 < len(args):
            extra_env = dict(extra_env, **named_env(args[i + 1]))
            i += 1
        elif token.startswith("-"):
            raise refuse(f"`rbox run {token}`: unknown option",
                         "usage: rbox run [--release] [--net] [--rw PATH] [--env NAME] -- PROGRAM [ARGS]")
        else:
            break
        i += 1
    if i >= len(args):
        raise refuse("usage: rbox run [--release] [--net] [--rw PATH] [--env NAME] -- PROGRAM [ARGS]")
    name, program_args = args[i], args[i + 1:]
    if "/" in name or name.startswith("."):
        raise refuse("give the program's name as cargo built it, not a path", "for example: rbox run -- recorder")
    bwrap, toolchain, project = open_project(dirs)
    profile = "release" if release else "debug"
    if not (project.target / profile / name).is_file():
        raise refuse(f"`{name}` has not been built ({profile})", "build it first: cargo build" + (" --release" if release else ""))
    cwd = Path.cwd().resolve()
    if not inside(cwd, project.root):
        cwd = project.root
    command = [f"/rbox/target/{profile}/{name}", *program_args]
    binds = []
    for raw in writable:
        path = (Path.cwd() / raw).resolve()
        if not path.is_dir():
            raise refuse(f"--rw {raw}: not an existing folder")
        if inside(path, project.root):
            relative = path.relative_to(project.root).parts
            if path == project.root or (relative and relative[0] in (".git", ".claude", ".cargo")):
                raise refuse(f"--rw {raw}: that part of the project stays read-only")
        else:
            for own in (dirs.share, dirs.cache, dirs.state):
                if inside(path, own) or inside(own, path):
                    raise refuse(f"--rw {raw}: that is one of rbox's own folders")
            if inside(dirs.home, path):
                raise refuse(f"--rw {raw}: too broad")
            require_human(f"run --rw {raw} (a folder outside the project)")
        binds.append(path)
    if net:
        return run_with_network(dirs, bwrap, toolchain, project, cwd, command, extra_env, binds)
    box = build_box(dirs, bwrap, toolchain, project, cwd, extra_env)
    for path in binds:
        box.add("--bind", str(path), str(path))
    box.exec(command)
    return 0


def cmd_export(dirs: Dirs, args: list) -> int:
    release = "--release" in args
    args = [a for a in args if a != "--release"]
    if len(args) != 3 or args[1] != "--to":
        raise refuse("usage: rbox export PROGRAM [--release] --to FOLDER")
    require_human("export")
    bwrap, toolchain, project = open_project(dirs)
    name = args[0]
    source = project.target / ("release" if release else "debug") / name
    if "/" in name or not source.is_file():
        raise refuse(f"`{name}` has not been built" + (" (release)" if release else " (debug)"))
    folder = Path(args[2]).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    shutil.copy2(source, dest)
    digest = sha256_file(dest)
    write_atomic(folder / (name + ".sha256"), f"{digest}  {name}\n")
    say(f"rbox: exported {dest}\n      sha256 {digest}\n      built with Rust {toolchain.name}; from here on it runs outside the box")
    return 0


def cmd_clean(dirs: Dirs, args: list) -> int:
    bwrap, toolchain, project = open_project(dirs)
    size = dir_size(project.target) // (1 << 20)
    shutil.rmtree(project.target, ignore_errors=True)
    project.target.mkdir(parents=True, exist_ok=True)
    say(f"rbox: removed {size} MB of build output for {project.root.name}")
    return 0


NEW_SETTINGS = """
# Added by `rbox new`. Loosen per crate if a rule does not fit.
[profile.release]
overflow-checks = true   # the release default is silent wrap-around
panic = "abort"          # a panic stops the process instead of leaving it half alive

[lints.rust]
unsafe_code = "forbid"

[lints.clippy]
arithmetic_side_effects = "deny"   # use checked_*, saturating_* or wrapping_* on purpose
unwrap_used = "deny"
"""


def cmd_new(dirs: Dirs, args: list) -> int:
    lib = "--lib" in args
    names = [a for a in args if a != "--lib"]
    if len(names) != 1 or names[0].startswith("-") or "/" in names[0]:
        raise refuse("usage: rbox new NAME [--lib]")
    name = names[0]
    dest = Path.cwd().resolve() / name
    if dest.exists():
        raise refuse(f"{dest} already exists")
    bwrap = find_bwrap()
    toolchain = dirs.toolchain()
    work = dirs.cache / f"new-{os.getpid()}"
    work.mkdir(parents=True)
    try:
        # cargo sees only this empty folder, never the folder you are standing in
        box = fetch_box(dirs, bwrap, toolchain, work, work, network=False)
        code = box.run([CARGO, "new", "--vcs", "none", *(["--lib"] if lib else []), name]).returncode
        if code != 0:
            return code
        made = work / name
        with open(made / "Cargo.toml", "a", encoding="utf-8") as manifest:
            manifest.write(NEW_SETTINGS)
        (made / "clippy.toml").write_text("allow-unwrap-in-tests = true\n", encoding="utf-8")
        shutil.move(str(made), str(dest))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    # write the lockfile now, so the first `cargo check` works straight away
    do_sync(dirs, bwrap, toolchain, Project(dirs, dest), quiet=True)
    say(f"rbox: created {dest} with overflow checks, panic = abort and the lints. Next: cd {name} && cargo check")
    return 0


def cmd_shell(dirs: Dirs, args: list) -> int:
    bwrap, toolchain, project = open_project(dirs)
    cwd = Path.cwd().resolve()
    box = build_box(dirs, bwrap, toolchain, project, cwd if inside(cwd, project.root) else project.root)
    box.env["PS1"] = "(rbox) $ "
    box.exec(["/bin/sh", *args])
    return 0


def named_env(name: str) -> dict:
    if name not in os.environ:
        raise refuse(f"--env {name}: that variable is not set")
    return {name: os.environ[name]}


# ---------------------------------------------------------------- selftest

SELFTEST_STAGES = ("build-script", "proc-macro", "test", "run")
SELFTEST_PROBES = [
    "read a file in your home folder",
    "see your home folder",
    "read the canary variable (getenv)",
    "read the canary variable (/proc/self/environ)",
    "read the canary variable (/proc/1/environ)",
    "see the caller's other variables (/proc/1/environ)",
    "read hidden project file .env",
    "read hidden project file .mcp.json",
    "read hidden project file .git/config",
    "read hidden project file sub/.env.local",
    "read hidden project file .claude/settings.local.json",
    "connect to the internet (TCP)",
    "send to the internet (UDP)",
    "connect to a service on this machine (TCP)",
    "connect to an abstract socket on this machine",
    "write into src/",
    "append to Cargo.toml",
    "plant a git hook",
    "write into the crate sources",
    "write into the toolchain",
    "write into /usr",
    "write into your home folder",
    "open your terminal (/dev/tty)",
    "create a nested sandbox",
    "control: sockets work inside the box",
    "control: ordinary files can be read",
]


def is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def host_sockets(dirs: Dirs) -> list:
    """Sockets on this machine that a hostile crate would love to reach."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    candidates = [
        "/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock", "/run/snapd.socket",
        "/run/dbus/system_bus_socket", "/tmp/.X11-unix/X0",
        f"{runtime}/bus", f"{runtime}/keyring/ssh", f"{runtime}/gnupg/S.gpg-agent", f"{runtime}/systemd/private",
        f"{runtime}/pipewire-0", f"{runtime}/wayland-0", str(dirs.home / ".1password" / "agent.sock"),
    ]
    if os.environ.get("SSH_AUTH_SOCK"):
        candidates.append(os.environ["SSH_AUTH_SOCK"])
    depth0 = runtime.rstrip("/").count("/")
    for current, subdirs, files in os.walk(runtime):
        if current.count("/") - depth0 >= 2:
            subdirs[:] = []
        candidates += [os.path.join(current, name) for name in files]
    found = []
    for path in candidates:
        real = os.path.realpath(path)
        if is_socket(real) and real not in found and '"' not in real and "\\" not in real:
            found.append(real)
    return found


def rust_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cmd_selftest(dirs: Dirs, args: list) -> int:
    import socket
    import threading

    bwrap = find_bwrap()
    toolchain = dirs.toolchain()
    template = Path(__file__).resolve().parent / "selftest"
    if not (template / "probes.rs").is_file():
        template = dirs.share / "selftest"
    if not (template / "probes.rs").is_file():
        raise cannot_run("the self-test crate is missing", "run rustbox/install.sh again")
    nonce = hashlib.sha256(os.urandom(16)).hexdigest()[:12]
    canary = f"rbox-canary-{nonce}"
    root = Path(tempfile.mkdtemp(prefix="rbox-selftest-")).resolve()
    canary_file = dirs.home / ".cache" / f"rbox-selftest-canary-{nonce}"
    project = Project(dirs, root)
    hits = {"tcp": 0, "abstract": 0}
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(8)
    abstract_name = f"rbox-selftest-{nonce}"
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix.bind("\0" + abstract_name)
    unix.listen(8)

    def accept(listener, key: str) -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            hits[key] += 1
            conn.close()

    for listener, key in ((tcp, "tcp"), (unix, "abstract")):
        threading.Thread(target=accept, args=(listener, key), daemon=True).start()
    sockets = host_sockets(dirs)
    has_tty = True
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
    except OSError:
        has_tty = False
    try:
        shutil.copytree(template, root, dirs_exist_ok=True)
        # secrets the box must hide, and a canary outside the project
        canary_file.parent.mkdir(parents=True, exist_ok=True)
        canary_file.write_text(canary + "\n", encoding="utf-8")
        for relative in (".env", ".mcp.json", ".git/config", "sub/.env.local", ".claude/settings.local.json"):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"SECRET={canary}\n", encoding="utf-8")
        (root / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
        config = [
            f"pub const CANARY: &str = {rust_str(canary)};",
            f"pub const NONCE: &str = {rust_str(nonce)};",
            f"pub const CANARY_FILE: &str = {rust_str(str(canary_file))};",
            'pub const CANARY_ENV: &str = "RBOX_SELFTEST_CANARY";',
            f"pub const HOME: &str = {rust_str(str(dirs.home))};",
            f"pub const ROOT: &str = {rust_str(str(root))};",
            f'pub const HOST_TCP: &str = "127.0.0.1:{tcp.getsockname()[1]}";',
            f"pub const ABSTRACT: &str = {rust_str(abstract_name)};",
            'pub const RESULTS: &str = "/rbox/target/selftest";',
            "pub const SOCKETS: &[&str] = &[" + ", ".join(rust_str(p) for p in sockets) + "];",
        ]
        (root / "probe_config.rs").write_text("\n".join(config) + "\n", encoding="utf-8")
        manifest_before = sha256_file(root / "Cargo.toml")
        os.environ["RBOX_SELFTEST_CANARY"] = canary  # if the box inherited our environment, the probes would see this

        # host controls: the things the probes look for really are there
        with socket.create_connection(("127.0.0.1", tcp.getsockname()[1]), timeout=2):
            pass
        time.sleep(0.05)
        baseline = dict(hits)
        if baseline["tcp"] != 1:
            raise cannot_run("self-test could not set up its own listener")

        say(f"rbox selftest: hostile crate in {root} ({len(sockets)} sockets on this machine to test against)")
        if do_sync(dirs, bwrap, toolchain, project, quiet=True) != 0:
            raise cannot_run("self-test could not prepare its scratch project")
        logs = []
        for step in (["build"], ["test"], ["run"]):
            box = build_box(dirs, bwrap, toolchain, project, root)
            done = box.run([CARGO, step[0], "--locked", *SOURCE_PINS, "--quiet"], capture=True)
            logs.append((step[0], done.returncode, done.stderr[-1500:]))
        time.sleep(0.1)

        results: dict = {}
        for stage in SELFTEST_STAGES:
            entries = {}
            try:
                for line in (project.target / "selftest" / f"{stage}.txt").read_text(encoding="utf-8").splitlines():
                    fields = line.split("\t")
                    if len(fields) >= 2:
                        entries[fields[0]] = (fields[1], fields[2] if len(fields) > 2 else "")
            except OSError:
                pass
            results[stage] = entries

        # what really happened on the host
        def host_changed(name: str, stage: str) -> bool:
            checks = {
                "write into src/": (root / "src" / f"rbox_probe_{stage}.rs").exists(),
                "append to Cargo.toml": sha256_file(root / "Cargo.toml") != manifest_before,
                "plant a git hook": (root / ".git" / "hooks" / "pre-commit").exists(),
                "write into the crate sources": (project.vendor / f"rbox_probe_{stage}").exists(),
                "write into the toolchain": (toolchain / "bin" / f"rbox_probe_{stage}").exists(),
                "write into /usr": os.path.exists(f"/usr/rbox_probe_{stage}"),
                "write into your home folder": (dirs.home / f"rbox_probe_{nonce}_{stage}").exists(),
            }
            return checks.get(name, False)

        expected = SELFTEST_PROBES + [f"connect to socket {p}" for p in sockets]
        leaked, broken, lines = [], [], []
        for name in expected:
            verdicts = []
            for stage in SELFTEST_STAGES:
                status, detail = results[stage].get(name, ("MISSING", "the probe did not run"))
                if status == "INBOX":
                    status = "LEAKED" if host_changed(name, stage) else "BLOCKED"
                if name == "connect to a service on this machine (TCP)" and hits["tcp"] > baseline["tcp"]:
                    status = "LEAKED"
                if name == "connect to an abstract socket on this machine" and hits["abstract"] > 0:
                    status = "LEAKED"
                verdicts.append((stage, status, detail))
            statuses = {status for _, status, _ in verdicts}
            control = name.startswith("control:")
            good = statuses == ({"OK"} if control else {"BLOCKED"})
            label = "ok     " if control and good else ("BLOCKED" if good else "FAILED ")
            note = ""
            if name == "open your terminal (/dev/tty)" and not has_tty:
                note = "   (rbox itself has no terminal here, so this proves little)"
            lines.append(f"  {label}  {name}{note}")
            if not good:
                for stage, status, detail in verdicts:
                    if status not in ("OK" if control else "BLOCKED",):
                        lines.append(f"           {stage}: {status} {detail}")
                        (leaked if status == "LEAKED" else broken).append(f"{name} [{stage}]")
        sys.stdout.write("\n".join(lines) + "\n")
        total = len(expected) * len(SELFTEST_STAGES)
        if leaked or broken:
            for step, code, tail in logs:
                if code != 0:
                    sys.stdout.write(f"\n  cargo {step} exited {code}:\n{tail}\n")
            sys.stdout.write(f"\nrbox selftest: FAILED. leaked: {len(leaked)}, did not run or broken: {len(broken)} (of {total} checks)\n")
            return 1
        sys.stdout.write(
            f"\nrbox selftest: passed. {len(expected)} probes x {len(SELFTEST_STAGES)} stages "
            f"(build script, proc macro, test, program) = {total} checks, nothing leaked.\n"
        )
        return 0
    finally:
        os.environ.pop("RBOX_SELFTEST_CANARY", None)
        tcp.close()
        unix.close()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(project.cache, ignore_errors=True)
        shutil.rmtree(project.state, ignore_errors=True)
        for stage in SELFTEST_STAGES:
            try:
                (dirs.home / f"rbox_probe_{nonce}_{stage}").unlink()
            except OSError:
                pass
        try:
            canary_file.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ doctor


def path_cargos(dirs: Dirs) -> tuple:
    """(is our shim first?, first cargo on PATH or None, other real cargos on PATH)."""
    ours = (dirs.share / "bin" / "rbox").resolve()
    hits = []
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(folder) / "cargo"
        if folder and (candidate.is_file() or candidate.is_symlink()) and candidate not in hits:
            hits.append(candidate)
    first = hits[0] if hits else None
    others = [h for h in hits if h.resolve() != ours]
    return bool(first and first.resolve() == ours), first, others


def box_failure_help(stderr: str) -> str:
    def sysctl(name: str) -> str:
        try:
            return Path("/proc/sys/" + name).read_text().strip()
        except OSError:
            return ""
    if sysctl("kernel/apparmor_restrict_unprivileged_userns") == "1":
        return ("this distro restricts sandboxes through AppArmor. A person runs: "
                "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0   (or adds an AppArmor profile for bwrap)")
    if sysctl("kernel/unprivileged_userns_clone") == "0":
        return "user namespaces are switched off. A person runs: sudo sysctl -w kernel.unprivileged_userns_clone=1"
    if sysctl("user/max_user_namespaces") == "0":
        return "user namespaces are switched off. A person runs: sudo sysctl -w user.max_user_namespaces=15000"
    if any(os.environ.get(m) for m in CLAUDE_MARKERS):
        return ("Claude Code's own sandbox blocks nested sandboxes by default. Run this from a normal terminal, "
                "or list `rbox` and `cargo` under sandbox.excludedCommands in ~/.claude/settings.json")
    return "bubblewrap could not create a sandbox: " + stderr.strip()[-300:]


def cmd_doctor(dirs: Dirs, args: list) -> int:
    results = []  # (ok, label, detail)
    apt = []

    def note(ok, label: str, detail: str) -> None:
        results.append((ok, label, detail))

    note(True, "python", sys.version.split()[0])
    bwrap = ""
    try:
        bwrap = find_bwrap()
        version = subprocess.run([bwrap, "--version"], capture_output=True, text=True).stdout.split()[-1]
        numbers = tuple(int(x) for x in version.split(".")[:3] if x.isdigit())
        note(numbers >= (0, 8, 0), "bubblewrap", version + ("" if numbers >= (0, 8, 0) else " (needs 0.8.0 or newer)"))
    except Stop:
        note(False, "bubblewrap", "not installed")
        apt.append("bubblewrap")
    box_ok = False
    if bwrap:
        box = Box(bwrap)
        box.system(full=True)
        box.env = {"PATH": "/usr/bin:/bin"}
        done = box.run(["/usr/bin/true"], capture=True)
        box_ok = done.returncode == 0
        note(box_ok, "sandbox starts", "yes" if box_ok else box_failure_help(done.stderr))
    if shutil.which("cc", path="/usr/bin:/bin"):
        note(True, "C linker (cc)", "found")
    else:
        note(False, "C linker (cc)", "not installed; rustc needs it to link")
        apt.append("gcc libc6-dev")
    toolchain = None
    try:
        toolchain = dirs.toolchain()
        note(True, "Rust toolchain", toolchain.name)
    except Stop as stop:
        note(False, "Rust toolchain", stop.message + "; " + stop.hint)
    if box_ok and toolchain:
        box = Box(bwrap)
        box.system(full=True)
        box.toolchain(toolchain)
        box.env["PATH"] = "/rbox/toolchain/bin:/usr/bin:/bin"
        script = "printf 'fn main() { println!(\"linked\"); }' > /tmp/m.rs && rustc /tmp/m.rs -o /tmp/m && /tmp/m"
        done = box.run(["/bin/sh", "-c", script], capture=True)
        ok = done.returncode == 0 and "linked" in done.stdout
        note(ok, "compile and link", "works in the box" if ok else done.stderr.strip()[-300:])
        if not ok and "libc6-dev" not in " ".join(apt):
            apt.append("libc6-dev")
    ours_first, first, others = path_cargos(dirs)
    if ours_first:
        note(True, "cargo on PATH", f"{first} -> rbox")
    elif first is None:
        note(False, "cargo on PATH", "no `cargo` on PATH; add ~/.local/bin to PATH (or re-run rustbox/install.sh)")
    else:
        note(False, "cargo on PATH", f"another cargo wins: {first}. Put ~/.local/bin first on PATH, or remove that one")
    for other in others:
        if ours_first:
            note(None, "unboxed cargo", f"{other} is also on PATH; it runs outside the box if called by its full path")
    if "--online" in args:
        for label, url in (("static.rust-lang.org", "https://static.rust-lang.org/dist/channel-rust-stable.toml.sha256"),
                           ("index.crates.io", INDEX_URL + "config.json")):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "rbox"}), timeout=15):
                    note(True, "reach " + label, "yes")
            except (urllib.error.URLError, OSError) as exc:
                note(False, "reach " + label, str(getattr(exc, "reason", exc)))
    failed = False
    for ok, label, detail in results:
        mark = "ok  " if ok else ("note" if ok is None else "FAIL")
        failed = failed or ok is False
        sys.stdout.write(f"  {mark}  {label:18} {detail}\n")
    if apt:
        sys.stdout.write("\nA person with sudo runs this once:\n  sudo apt install " + " ".join(sorted(set(" ".join(apt).split()))) + "\n")
    sys.stdout.write("\n" + ("rbox: this machine is NOT ready\n" if failed else "rbox: this machine is ready\n"))
    return EXIT_CANNOT_RUN if failed else 0


# -------------------------------------------------------------------- main

USAGE = """rbox: build, test and run Rust inside a box.

  cargo ...                 the usual commands, boxed (`cargo` on PATH is rbox)
  rbox sync                 download the crates the lockfile names (nothing runs)
  rbox allow [--show]       review and approve crates that are waiting (a person, own terminal)
  rbox urgent NAME@VERSION  pull one version past the cooldown (a person)
  rbox status               where things are and what is waiting
  rbox warm                 pre-build after dependency changes
  rbox run -- PROGRAM       run a built program in the box
  rbox export PROGRAM --to FOLDER   copy a built program out of the box (a person)
  rbox new NAME [--lib]     create a project with the safety settings
  rbox clean                empty this project's build output
  rbox doctor [--online]    is this machine ready
  rbox selftest             prove the box holds, with a hostile crate
"""


def main(argv: list) -> int:
    dirs = Dirs(dict(os.environ))
    if dirs.test_world:
        say("rbox: note: test world (RBOX_TEST_BASE); approvals here are not this machine's")
    args = argv[1:]
    if os.path.basename(argv[0]) == "cargo":
        return cmd_cargo(dirs, args, False, {})
    net = False
    extra_env: dict = {}
    while args and args[0] in ("--net", "--env"):
        if args[0] == "--net":
            net = True
            args = args[1:]
        else:
            if len(args) < 2:
                raise refuse("--env needs a variable name")
            extra_env.update(named_env(args[1]))
            args = args[2:]
    if not args or args[0] in ("help", "--help", "-h"):
        sys.stdout.write(USAGE)
        return 0
    command, rest = args[0], args[1:]
    if command == "cargo":
        return cmd_cargo(dirs, rest, net, extra_env)
    if command == "run":
        return cmd_run(dirs, rest, net, extra_env)
    simple = {
        "sync": cmd_sync, "allow": cmd_allow, "urgent": cmd_urgent, "status": cmd_status, "warm": cmd_warm,
        "export": cmd_export, "clean": cmd_clean, "new": cmd_new, "shell": cmd_shell, "doctor": cmd_doctor,
        "toolchain": cmd_toolchain, "selftest": cmd_selftest,
    }
    if command in simple:
        return simple[command](dirs, rest)
    raise refuse(f"`rbox {command}` is not a command", "see `rbox help`")


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Stop as stop:
        say(f"rbox: {KIND.get(stop.code, 'error')}: {stop.message}")
        if stop.hint:
            say(f"      {stop.hint}")
        sys.exit(stop.code)
    except KeyboardInterrupt:
        sys.exit(130)
