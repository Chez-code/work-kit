// The probes every stage runs (build script, proc macro, test, program).
// Each one tries something a hostile crate would try and records the outcome
// as a line `name<TAB>status<TAB>detail` in /rbox/target/selftest/<stage>.txt.
//
// status: BLOCKED / LEAKED for attacks, OK / FAIL for controls that must work,
//         INBOX for a write that succeeded inside the box (rbox then checks on
//         the host that nothing real changed).
// probe_config.rs is written by `rbox selftest` for each run.
#![allow(dead_code)]

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::os::linux::net::SocketAddrExt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

include!("probe_config.rs");

unsafe extern "C" {
    fn unshare(flags: i32) -> i32;
}

const CLONE_NEWUSER: i32 = 0x1000_0000;

struct Report {
    lines: Vec<String>,
}

impl Report {
    fn attack(&mut self, name: &str, leaked: bool, detail: &str) {
        let status = if leaked { "LEAKED" } else { "BLOCKED" };
        self.lines.push(format!("{name}\t{status}\t{}", detail.replace(['\n', '\t'], " ")));
    }
    fn control(&mut self, name: &str, ok: bool, detail: &str) {
        let status = if ok { "OK" } else { "FAIL" };
        self.lines.push(format!("{name}\t{status}\t{}", detail.replace(['\n', '\t'], " ")));
    }
    fn write_attempt(&mut self, name: &str, path: &str, marker: &str, append: bool) {
        if let Some(parent) = Path::new(path).parent() {
            let _ = fs::create_dir_all(parent);
        }
        let result = if append {
            fs::OpenOptions::new().append(true).open(path).and_then(|mut f| f.write_all(marker.as_bytes()))
        } else {
            fs::write(path, marker)
        };
        match result {
            Ok(()) => self.lines.push(format!("{name}\tINBOX\twrote {path} inside the box")),
            Err(err) => self.lines.push(format!("{name}\tBLOCKED\t{err}")),
        }
    }
}

fn file_has(path: &str, needle: &str) -> (bool, String) {
    match fs::read(path) {
        Ok(bytes) => {
            let found = bytes.windows(needle.len()).any(|w| w == needle.as_bytes());
            (found, format!("readable, {} bytes", bytes.len()))
        }
        Err(err) => (false, err.to_string()),
    }
}

fn tcp(target: &str) -> (bool, String) {
    let addr: SocketAddr = match target.parse() {
        Ok(a) => a,
        Err(err) => return (false, format!("bad address: {err}")),
    };
    match TcpStream::connect_timeout(&addr, Duration::from_secs(2)) {
        Ok(_) => (true, format!("connected to {target}")),
        Err(err) => (false, err.to_string()),
    }
}

// CONNECT through the in-box proxy; true only when the tunnel was opened.
fn through_proxy(proxy: &str, target: &str) -> (bool, String) {
    let mut stream = match TcpStream::connect(proxy) {
        Ok(s) => s,
        Err(err) => return (false, format!("no proxy: {err}")),
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(15)));
    if let Err(err) = write!(stream, "CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n") {
        return (false, err.to_string());
    }
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") && head.len() < 4096 {
        match stream.read(&mut byte) {
            Ok(1) => head.push(byte[0]),
            _ => break,
        }
    }
    let status = String::from_utf8_lossy(&head).lines().next().unwrap_or("no answer").to_string();
    (status.contains(" 200 "), status)
}

pub fn run(stage: &str) {
    let mut r = Report { lines: Vec::new() };
    let marker = format!("{CANARY}-{stage}\n");

    // ---- reading secrets
    let (found, detail) = file_has(CANARY_FILE, CANARY);
    r.attack("read a file in your home folder", found, &detail);
    let seen: Vec<&str> = [".ssh", ".config", ".bashrc", ".gnupg", ".1password", ".aws", ".claude", ".local"]
        .into_iter()
        .filter(|name| Path::new(&format!("{HOME}/{name}")).exists())
        .collect();
    r.attack("see your home folder", !seen.is_empty(), &format!("visible: {seen:?}"));
    r.attack("read the canary variable (getenv)", std::env::var(CANARY_ENV).is_ok(), "");
    for path in ["/proc/self/environ", "/proc/1/environ"] {
        let (found, detail) = file_has(path, CANARY);
        r.attack(&format!("read the canary variable ({path})"), found, &detail);
    }
    let (claude, detail) = file_has("/proc/1/environ", "CLAUDE");
    r.attack("see the caller's other variables (/proc/1/environ)", claude, &detail);
    for hidden in [
        ".env",
        ".mcp.json",
        ".git/config",
        "sub/.env.local",
        ".claude/settings.local.json",
        "vendor/pkg/.env",
        "target/debug/.env",
        "web/node_modules/pkg/.env",
        "tools/venv/.env",
    ] {
        let (found, detail) = file_has(&format!("{ROOT}/{hidden}"), CANARY);
        r.attack(&format!("read hidden project file {hidden}"), found, &detail);
    }

    // ---- reaching out
    let (ok, detail) = tcp("1.1.1.1:443");
    r.attack("connect to the internet (TCP)", ok, &detail);
    let udp = UdpSocket::bind("0.0.0.0:0").and_then(|s| s.send_to(b"x", "1.1.1.1:53"));
    r.attack("send to the internet (UDP)", udp.is_ok(), &format!("{udp:?}"));
    let (ok, detail) = tcp(HOST_TCP);
    r.attack("connect to a service on this machine (TCP)", ok, &detail);
    let abstract_result = std::os::unix::net::SocketAddr::from_abstract_name(ABSTRACT.as_bytes())
        .and_then(|addr| UnixStream::connect_addr(&addr));
    r.attack("connect to an abstract socket on this machine", abstract_result.is_ok(), &format!("{:?}", abstract_result.err()));
    for socket in SOCKETS {
        let result = UnixStream::connect(socket);
        r.attack(&format!("connect to socket {socket}"), result.is_ok(), &format!("{:?}", result.err()));
    }

    // ---- writing where it would matter later
    r.write_attempt("write into src/", &format!("{ROOT}/src/rbox_probe_{stage}.rs"), &marker, false);
    r.write_attempt("append to Cargo.toml", &format!("{ROOT}/Cargo.toml"), &marker, true);
    r.write_attempt("plant a git hook", &format!("{ROOT}/.git/hooks/pre-commit"), &marker, false);
    r.write_attempt("write into the crate sources", &format!("/rbox/vendor/rbox_probe_{stage}"), &marker, false);
    r.write_attempt("write into the toolchain", &format!("/rbox/toolchain/bin/rbox_probe_{stage}"), &marker, false);
    r.write_attempt("write into /usr", &format!("/usr/rbox_probe_{stage}"), &marker, false);
    r.write_attempt("write into your home folder", &format!("{HOME}/rbox_probe_{NONCE}_{stage}"), &marker, false);

    // ---- while compiling there must be no proxy in sight, even when the user asked for `--net`
    if stage == "build-script" || stage == "proc-macro" {
        let seen: Vec<&str> = ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "RBOX_PROXY"]
            .into_iter()
            .filter(|name| std::env::var(name).is_ok())
            .collect();
        let relay = tcp("127.0.0.1:3128").0;
        r.attack("see the network proxy while compiling", !seen.is_empty() || relay, &format!("variables: {seen:?}, relay reachable: {relay}"));
    }

    // ---- only with `--net`: the proxy must pass the listed host and nothing else
    if let Ok(proxy) = std::env::var("HTTPS_PROXY") {
        let proxy = proxy.trim_start_matches("http://").to_string();
        let (ok, detail) = through_proxy(&proxy, HOST_TCP);
        r.control("control: the listed host is reachable through the proxy", ok, &detail);
        let (ok, detail) = through_proxy(&proxy, "1.1.1.1:443");
        r.attack("reach an unlisted host through the proxy", ok, &detail);
        let (ok, detail) = through_proxy(&proxy, HOST_TCP_UNLISTED);
        r.attack("reach an unlisted service on this machine through the proxy", ok, &detail);
    }

    // ---- terminal and nesting
    let tty = fs::OpenOptions::new().read(true).write(true).open("/dev/tty");
    r.attack("open your terminal (/dev/tty)", tty.is_ok(), &format!("{:?}", tty.err()));
    let nested = unsafe { unshare(CLONE_NEWUSER) };
    r.attack("create a nested sandbox", nested == 0, &format!("unshare returned {nested}"));

    // ---- controls: these must work, or the probes above prove nothing
    let listener = TcpListener::bind("127.0.0.1:0");
    let loopback = match &listener {
        Ok(l) => l.local_addr().ok().and_then(|a| TcpStream::connect_timeout(&a, Duration::from_secs(2)).ok()).is_some(),
        Err(_) => false,
    };
    r.control("control: sockets work inside the box", loopback, "");
    let mut readable = String::new();
    let usr = fs::File::open("/usr/lib/os-release").and_then(|mut f| f.read_to_string(&mut readable)).is_ok();
    r.control("control: ordinary files can be read", usr, "");

    let dir = format!("{RESULTS}");
    let wrote = fs::create_dir_all(&dir)
        .and_then(|_| fs::write(format!("{dir}/{stage}.txt"), r.lines.join("\n") + "\n"));
    if let Err(err) = wrote {
        // No results file means rbox reports this stage as not run.
        eprintln!("rbox selftest: could not write results for {stage}: {err}");
    }
}
