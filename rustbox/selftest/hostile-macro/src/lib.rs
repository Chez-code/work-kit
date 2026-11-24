use proc_macro::TokenStream;

#[path = "../../probes.rs"]
mod probes;

/// Runs the probes while the compiler expands the macro, then expands to nothing.
#[proc_macro]
pub fn probe(_input: TokenStream) -> TokenStream {
    probes::run("proc-macro");
    TokenStream::new()
}
