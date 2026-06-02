//! GameStream client (Rust rewrite — in progress).
//!
//! Phase: validating that the H.264 stream produced by the Python host
//! (libx264 baseline, Annex-B) can be decoded in Rust without FFmpeg or the
//! MSVC toolchain. See `tests/decode.rs`.

/// Decode an Annex-B H.264 byte stream, returning (frames_decoded, last_dims).
/// Kept here so it can be reused by the future receive/render pipeline.
pub fn decode_annexb(stream: &[u8]) -> (usize, Option<(usize, usize)>) {
    use openh264::decoder::Decoder;
    use openh264::formats::YUVSource;
    use openh264::nal_units;

    let mut decoder = match Decoder::new() {
        Ok(d) => d,
        Err(_) => return (0, None),
    };

    let mut frames = 0usize;
    let mut dims = None;
    for nal in nal_units(stream) {
        if let Ok(Some(frame)) = decoder.decode(nal) {
            dims = Some(frame.dimensions());
            frames += 1;
        }
    }
    (frames, dims)
}
