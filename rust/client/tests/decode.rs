//! Validates that the Rust decoder handles the exact H.264 stream that
//! host.py emits (libx264 baseline, Annex-B). Test vector generated from the
//! project's own encoder.

use gamestream_client::decode_annexb;

#[test]
fn decodes_python_baseline_stream() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/data/h264_baseline_320x240.h264"
    );
    let data = std::fs::read(path).expect("test vector missing");
    assert!(data.len() > 1000, "vector too small");

    let (frames, dims) = decode_annexb(&data);
    eprintln!("decoded {frames} frames, dims = {dims:?}");

    assert!(frames > 0, "decoder produced no frames");
    assert_eq!(dims, Some((320, 240)), "wrong decoded dimensions");
}
