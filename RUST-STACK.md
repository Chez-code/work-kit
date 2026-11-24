# RUST-STACK — why the box exists, and what to build on

Notes for a team moving to Rust for security reasons. `RUST-SETUP.md` is how
to install and operate rbox; this file is the reasoning and the stack advice
behind it. Facts were checked against primary sources on 2026-11-23.

## What Rust fixes, and what it does not

Rust fixes:

- One binary on the server. No interpreter, nothing installed at deploy time.
- Data races, null values and ignored errors stop compiling.
- With your own types, mixing up two quantities (a price and a size, say)
  stops compiling too.
- No garbage collector, so timing stays steady.

Rust does not fix:

- **Poisoned packages.** Crates run code on your machine while compiling
  (build scripts and proc macros), the same way Python installers do.
- Keys stolen from a compromised machine.
- Logic bugs.

C# and Python are already memory-safe. The gain from Rust is a smaller attack
surface and a stricter compiler, and it only pays off with the rules below.

## What happened, with dates

| Date | What |
|---|---|
| 2026-09-17 | The Rust team warned of ongoing attacks on owners of popular crates; the lure is a fake job or contract video call. [Rust blog](https://blog.rust-lang.org/2026/09/17/targeted-attacks/) |
| 2026-08-20 | `arrayref`, `internment` and `append-only-vec`, all real and popular, were hijacked through a compromised maintainer account. The bad versions pulled in a fake crate whose build script downloaded malware. Live for 86 to 107 minutes. [Rust blog](https://blog.rust-lang.org/2026/08/20/supply-chain-attack-on-arrayref/) |
| 2026-05 | TrapDoor: more than 34 packages across npm, PyPI and crates.io aimed at crypto developers. The crates ran at compile time and looked for wallet keystores. [The Hacker News, citing Socket](https://thehackernews.com/2026/05/trapdoor-supply-chain-attack-spreads.html) |
| 2026-03-24 | LiteLLM on PyPI: a compromised CI scanner leaked the publish token, and two releases shipped an info-stealer. [Snyk](https://snyk.io/articles/poisoned-security-scanner-backdooring-litellm) |
| 2025-09-24 | `faster_log` and `async_println`: fake logging crates that searched source files for Solana and Ethereum private keys. [Rust blog](https://blog.rust-lang.org/2025/09/24/crates.io-malicious-crates-fasterlog-and-asyncprintln) |

The attackers want what a developer machine has: keys. Most of these ran while
compiling, before any of your own code started.

## The rules, and how rbox enforces each

| Rule | What it stops | In rbox |
|---|---|---|
| Building runs in a box | a build script or proc macro reading your files or calling home | no home folder, no network, no caller environment, project read-only |
| Downloading runs nothing | project config making a program run during `cargo fetch` or `update` | only cargo runs; the project's `.cargo/` is hidden at every level |
| A 7-day cooldown | a hijacked release that is caught within days | Cargo's `min-publish-age` (stable since Rust 1.100, 2026-11-12), pinned on the command line, plus rbox's own check at approval time |
| Exact files are approved by a person | a new or changed crate slipping in unseen | every locked crate file (name, version, checksum) must be in the machine's confirmed set |
| The session cannot approve itself | an AI assistant adding a dependency on its own | `rbox allow` needs a real terminal and refuses under a Claude session |
| Network is a list | a test or program reaching anything it likes | `--net` passes only listed `host:port` pairs, through a proxy outside the box |

The August 2026 hijack would have been stopped three times over: the cooldown
(under two hours live), the approval step (a new version of an approved crate,
and a new crate name it pulled in), and the box (its build script could not
download anything).

For teammates who build with plain cargo rather than rbox, the cooldown alone
is two lines in `.cargo/config.toml` or `~/.cargo/config.toml`. Inside rbox
this file is not used; rbox pins the same setting itself.

```toml
[registry]
global-min-publish-age = "7 days"
```

A project file can switch that off again (`[registries.crates-io]
min-publish-age = "0"` does it), which is why rbox pins three settings on the
command line instead of trusting a file.

## Splitting a system that holds keys

The same idea one level up: give each program only what it needs.

| Program | Talks to the network | Holds keys |
|---|---|---|
| Feed: collectors and recorders | yes, to listed hosts | read-only API keys |
| Logic: strategy, state, risk, analytics | no | none |
| Signer: checks limits, signs, sends | only to the venue | signing keys |

A bad crate in the feed or the logic finds no signing keys, and in the logic
no way out. The worst it can do is ask for something, and the signer's limits
(size, allowed targets, rate, daily loss, a kill switch) cap that. The signer
is the smallest program with the fewest dependencies. `rbox --net` with a
short host list is the development-time version of the feed's restriction.

## What to build on

Keep the foundation small and boring, and write the domain-specific parts
yourselves. Do not write your own cryptography, TLS or async runtime.

| Job | Pick | Why |
|---|---|---|
| Async runtime | `tokio` | the standard; everything else expects it |
| TLS | `rustls` | pure Rust, no OpenSSL |
| HTTP | `reqwest` with default features off | honours `HTTPS_PROXY` on its own; drop to `hyper` if it drags in too much |
| WebSocket | `tokio-tungstenite` | plain and proven |
| JSON | `serde`, `serde_json` | swap for a SIMD parser only on a measured hot path |
| Signing | `ed25519-dalek`, `k256`, `sha2`, `sha3`, `zeroize` | audited primitives |
| Logs | `tracing` | structured logs with timings |
| Tests | `proptest`, `cargo-fuzz`, `insta` | property tests, fuzzing for parsers, snapshots for replays |

Count the real dependency tree with `cargo tree` before committing to any of
these, and expect the first `rbox allow` for an async stack to list 150 to
300 crate files. After that, an update is a short list of changes.

## Settings for code that handles money

`rbox new` writes these into a new project:

```toml
[profile.release]
overflow-checks = true   # the release default is silent wrap-around
panic = "abort"          # a panic stops the process instead of leaving it half alive

[lints.rust]
unsafe_code = "forbid"

[lints.clippy]
arithmetic_side_effects = "deny"   # use checked_*, saturating_* or wrapping_* on purpose
unwrap_used = "deny"
```

Alongside them: prices and sizes as integers in their own types, never
floats; one deterministic core that takes an event and returns actions, with
no clock and no network inside it; and tests that replay recorded real days
and compare against a saved result.

## Going through the proxy

With `--net`, the box reaches the outside only through `HTTPS_PROXY`.
`reqwest` uses it automatically. A raw TCP or WebSocket client has to open
its connection through the proxy first. This is the whole step, standard
library only; hand the returned stream to your TLS and WebSocket layers.

```rust
use std::io::{Read, Write};
use std::net::TcpStream;

/// Open a TCP connection to `target` ("host:port"), through HTTPS_PROXY when it is set.
fn dial(target: &str) -> std::io::Result<TcpStream> {
    let Ok(proxy) = std::env::var("HTTPS_PROXY") else {
        return TcpStream::connect(target);
    };
    let mut stream = TcpStream::connect(proxy.trim_start_matches("http://"))?;
    write!(stream, "CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n")?;
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        stream.read_exact(&mut byte)?;
        head.push(byte[0]);
    }
    if !head.starts_with(b"HTTP/1.1 200") {
        let status = String::from_utf8_lossy(&head).lines().next().unwrap_or("").to_string();
        return Err(std::io::Error::other(format!("proxy refused {target}: {status}")));
    }
    Ok(stream)
}
```

Going through an allow-listing proxy is also how the feed should run in
production, so this code is not test scaffolding.

## Not covered yet

Known-vulnerability checks (`cargo-deny` with the RustSec database), shared
review records (`cargo-vet`), static release builds for copying to other
machines, and service templates that give a deployed program the same
restrictions the box gives it in development.
