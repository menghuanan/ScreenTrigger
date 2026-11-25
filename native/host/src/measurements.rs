//! Opt-in monotonic observations for the installed-candidate stop test.
use std::sync::OnceLock;
use std::time::Instant;

pub(crate) fn mark(event: &str, execution: &str) {
    static START: OnceLock<Option<Instant>> = OnceLock::new();
    if let Some(origin) = START.get_or_init(|| {
        (std::env::var("ST_MEASURE_TIMING").as_deref() == Ok("1")).then(Instant::now)
    }) {
        eprintln!("[gate-timing] {event} {execution} {}", origin.elapsed().as_nanos());
    }
}
