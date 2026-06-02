//! UDP video receiver: reassembles chunked frames, decrypts, decodes (openh264).
//! Mirrors client.py's VideoReceiver. Produces stats and, optionally, decoded
//! RGBA frames for a renderer.

use std::collections::HashMap;
use std::net::UdpSocket;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use openh264::decoder::Decoder;
use openh264::formats::YUVSource;
use openh264::nal_units;

use crate::crypto::SessionCipher;
use crate::protocol::parse_chunk;

#[derive(Default)]
pub struct Stats {
    pub frames: AtomicUsize,
    pub errors: AtomicUsize,
    pub dims: Mutex<Option<(usize, usize)>>,
}

/// A decoded frame as tightly-packed RGBA, for the renderer.
pub struct FrameBuf {
    pub w: usize,
    pub h: usize,
    pub rgba: Vec<u8>,
    pub seq: u64,
}

/// Latest-frame slot shared with the renderer (None until the first frame).
pub type FrameSink = Arc<Mutex<Option<FrameBuf>>>;

/// Bind `port` and decode incoming video on a background thread until `running`
/// is cleared. If `sink` is given, each decoded frame is converted to RGBA and
/// stored there (latest wins).
pub fn spawn_receiver(
    port: u16,
    cipher: Option<SessionCipher>,
    running: Arc<AtomicBool>,
    sink: Option<FrameSink>,
) -> std::io::Result<Arc<Stats>> {
    let sock = UdpSocket::bind(("0.0.0.0", port))?;
    sock.set_read_timeout(Some(Duration::from_millis(100)))?;

    let stats = Arc::new(Stats::default());
    let stats_thread = stats.clone();

    std::thread::spawn(move || {
        let mut decoder = match Decoder::new() {
            Ok(d) => d,
            Err(_) => return,
        };
        let mut pending: HashMap<u32, (u32, HashMap<u32, Vec<u8>>)> = HashMap::new();
        let mut latest: i64 = -1;
        let mut seq: u64 = 0;
        let mut rgb = Vec::new();
        let mut buf = [0u8; 2048];

        while running.load(Ordering::Relaxed) {
            let n = match sock.recv_from(&mut buf) {
                Ok((n, _)) => n,
                Err(ref e)
                    if e.kind() == std::io::ErrorKind::WouldBlock
                        || e.kind() == std::io::ErrorKind::TimedOut =>
                {
                    continue
                }
                Err(_) => break,
            };

            let (h, payload) = match parse_chunk(&buf[..n]) {
                Some(v) => v,
                None => continue,
            };
            if (h.frame_id as i64) < latest - 2 {
                continue;
            }

            let entry = pending
                .entry(h.frame_id)
                .or_insert_with(|| (h.total_chunks, HashMap::new()));
            entry.1.insert(h.chunk_idx, payload.to_vec());
            if entry.1.len() as u32 != entry.0 {
                continue;
            }

            let total = entry.0;
            let mut blob = Vec::new();
            for i in 0..total {
                if let Some(c) = entry.1.get(&i) {
                    blob.extend_from_slice(c);
                }
            }
            latest = h.frame_id as i64;
            pending.retain(|&k, _| k as i64 > latest);

            let frame_data = match &cipher {
                Some(c) => match c.decrypt(&blob) {
                    Some(p) => p,
                    None => {
                        stats_thread.errors.fetch_add(1, Ordering::Relaxed);
                        continue;
                    }
                },
                None => blob,
            };

            for nal in nal_units(&frame_data) {
                match decoder.decode(nal) {
                    Ok(Some(frame)) => {
                        let (w, hgt) = frame.dimensions();
                        *stats_thread.dims.lock().unwrap() = Some((w, hgt));
                        stats_thread.frames.fetch_add(1, Ordering::Relaxed);

                        if let Some(sink) = &sink {
                            rgb.resize(w * hgt * 3, 0);
                            frame.write_rgb8(&mut rgb);
                            let mut rgba = vec![0u8; w * hgt * 4];
                            for i in 0..w * hgt {
                                rgba[i * 4] = rgb[i * 3];
                                rgba[i * 4 + 1] = rgb[i * 3 + 1];
                                rgba[i * 4 + 2] = rgb[i * 3 + 2];
                                rgba[i * 4 + 3] = 255;
                            }
                            seq += 1;
                            *sink.lock().unwrap() = Some(FrameBuf { w, h: hgt, rgba, seq });
                        }
                    }
                    Ok(None) => {}
                    Err(_) => {
                        stats_thread.errors.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }
        }
    });

    Ok(stats)
}
