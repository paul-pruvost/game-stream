//! GameStream client (Rust rewrite — in progress).
//!
//! Network + decode pipeline, interoperable with the Python `host.py`:
//!   - control: TLS/TCP, length-prefixed JSON messages (handshake / config)
//!   - video:   UDP, [16B chunk header][payload], whole frame AES-256-GCM
//!   - decode:  openh264 (no FFmpeg / MSVC)
//!
//! GPU rendering / input capture come next; this layer is verifiable headless.

pub mod crypto;
pub mod net;
pub mod protocol;
pub mod video;

/// Decode an Annex-B H.264 byte stream, returning (frames_decoded, last_dims).
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
