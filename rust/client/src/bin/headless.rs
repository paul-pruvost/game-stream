//! Headless GameStream client probe: connects to a Python host.py over
//! TLS+UDP, decodes video, and reports throughput. Proves the Rust
//! network+decode pipeline interoperates with the existing host.
//!
//! Usage: gamestream-headless [host] [--port 9900] [--video-port 9901] [--secs 5]

use std::io::Write;
use std::net::TcpStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde_json::json;

use gamestream_client::crypto::{hex_decode, SessionCipher};
use gamestream_client::protocol::{recv_message, send_message, MSG_CONFIG, MSG_HANDSHAKE};
use gamestream_client::video::spawn_receiver;

fn main() {
    let mut host = "127.0.0.1".to_string();
    let mut port: u16 = 9900;
    let mut video_port: u16 = 9901;
    let mut secs: u64 = 5;

    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--port" if i + 1 < args.len() => { port = args[i + 1].parse().unwrap_or(port); i += 2; }
            "--video-port" if i + 1 < args.len() => { video_port = args[i + 1].parse().unwrap_or(video_port); i += 2; }
            "--secs" if i + 1 < args.len() => { secs = args[i + 1].parse().unwrap_or(secs); i += 2; }
            s if !s.starts_with("--") => { host = s.to_string(); i += 1; }
            _ => i += 1,
        }
    }

    // ── TLS control connection (accept self-signed cert, like the Python client) ──
    let connector = native_tls::TlsConnector::builder()
        .danger_accept_invalid_certs(true)
        .danger_accept_invalid_hostnames(true)
        .build()
        .expect("tls connector");

    let tcp = TcpStream::connect((host.as_str(), port)).expect("tcp connect");
    tcp.set_nodelay(true).ok();
    let mut tls = connector
        .connect(&host, tcp)
        .expect("tls handshake");

    send_message(
        &mut tls,
        &json!({
            "type": MSG_HANDSHAKE,
            "magic": "GSTR",
            "version": "2.0",
            "video_port": video_port,
            "audio_port": video_port + 1,
        }),
    )
    .expect("send handshake");

    let cfg = recv_message(&mut tls).expect("recv config");
    if cfg.get("type").and_then(|v| v.as_i64()) != Some(MSG_CONFIG) {
        eprintln!("unexpected reply: {cfg}");
        std::process::exit(1);
    }

    let width = cfg.get("width").and_then(|v| v.as_i64()).unwrap_or(0);
    let height = cfg.get("height").and_then(|v| v.as_i64()).unwrap_or(0);
    let codec = cfg.get("codec").and_then(|v| v.as_str()).unwrap_or("?");
    let encrypted = cfg.get("encrypted").and_then(|v| v.as_bool()).unwrap_or(false);
    println!("  connected: {width}x{height} codec={codec} encrypted={encrypted}");

    let cipher = if encrypted {
        cfg.get("session_key")
            .and_then(|v| v.as_str())
            .and_then(hex_decode)
            .and_then(|k| SessionCipher::new(&k))
    } else {
        None
    };
    if encrypted && cipher.is_none() {
        eprintln!("FAIL: encrypted stream but no usable session key");
        std::process::exit(1);
    }

    // ── Video receive + decode ──
    let running = Arc::new(AtomicBool::new(true));
    let stats = spawn_receiver(video_port, cipher, running.clone()).expect("bind video");

    // Keep the control connection alive so the host keeps streaming to us.
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(secs) {
        std::thread::sleep(Duration::from_millis(100));
        let _ = tls.write(&[]); // no-op keepalive; host tolerates silence
    }
    running.store(false, Ordering::Relaxed);
    std::thread::sleep(Duration::from_millis(150));

    let frames = stats.frames.load(Ordering::Relaxed);
    let errors = stats.errors.load(Ordering::Relaxed);
    let dims = *stats.dims.lock().unwrap();
    let fps = frames as f64 / secs as f64;

    println!("\n  frames decoded : {frames}  ({fps:.1} fps over {secs}s)");
    println!("  decode errors  : {errors}");
    println!("  last frame     : {dims:?}");

    let ok = frames > (secs as usize) * 5 && dims.is_some();
    println!("\nRESULT: {}", if ok { "PIPELINE OK" } else { "PIPELINE FAIL" });
    std::process::exit(if ok { 0 } else { 1 });
}
