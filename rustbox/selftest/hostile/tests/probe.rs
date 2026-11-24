#[path = "../../probes.rs"]
mod probes;

#[test]
fn probe() {
    probes::run("test");
}
