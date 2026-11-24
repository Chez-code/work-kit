// Runs at build time, like a hostile build script would.
#[path = "../probes.rs"]
mod probes;

fn main() {
    // A hostile build script would ask to be re-run the moment a proxy appears in the
    // environment, hoping to be compiled again inside a box that has the network.
    println!("cargo:rerun-if-env-changed=HTTPS_PROXY");
    println!("cargo:rerun-if-env-changed=ALL_PROXY");
    probes::run("build-script");
}
