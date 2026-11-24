#[path = "../../probes.rs"]
mod probes;

// Expands at compile time: the proc macro runs its probes inside rustc.
hostile_macro::probe!();

fn main() {
    probes::run("run");
}
