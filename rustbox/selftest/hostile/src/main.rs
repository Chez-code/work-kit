#[path = "../../probes.rs"]
mod probes;

// Expands at compile time: the proc macro runs its probes inside rustc.
hostile_macro::probe!();

fn main() {
    // `rbox selftest` runs this once plainly and once with `--net`
    let stage = std::env::args().nth(1).unwrap_or_else(|| "run".to_string());
    probes::run(&stage);
}
