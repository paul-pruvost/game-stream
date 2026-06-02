//! UDP video receiver: reassembles chunked frames, decrypts, decodes (openh264).
//! Mirrors client.py's VideoReceiver but is decode-to-stats only (no rendering yet).

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

/// Bind `port` and decode incoming video on a background thread until `running`
/// is cleared. Returns shared stats.
pub fn spawn_receiver(
    port: u16,
    cipher: Option<SessionCipher>,
    running: Arc<AtomicBool>,
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
        // frame_id -> (total_chunks, idx -> payload)
        let mut pending: HashMap<u32, (u32, HashMap<u32, Vec<u8>>)> = HashMap::new();
        let mut latest: i64 = -1;
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
                continue; // too old
            }

            let entry = pending
                .entry(h.frame_id)
                .or_insert_with(|| (h.total_chunks, HashMap::new()));
            entry.1.insert(h.chunk_idx, payload.to_vec());

            if entry.1.len() as u32 == entry.0 {
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
                            *stats_thread.dims.lock().unwrap() = Some(frame.dimensions());
                            stats_thread.frames.fetch_add(1, Ordering::Relaxed);
                        }
                        Ok(None) => {}
                        Err(_) => {
                            stats_thread.errors.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
            }
        }
    });

    Ok(stats)
}
