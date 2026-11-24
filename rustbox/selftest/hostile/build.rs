// Runs at build time, like a hostile build script would.
#[path = "../probes.rs"]
mod probes;

fn main() {
    probes::run("build-script");
}
