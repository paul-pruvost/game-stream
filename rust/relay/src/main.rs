//! GameStream Internet Relay Server — Rust/tokio rewrite (TCP binary relay).
//!
//! Wire protocol (identical to the Python relay.py, so it is a drop-in
//! replacement for `python host.py --relay` / `python client.py --relay`):
//!
//!   1. Client connects over TCP and sends one line of JSON, terminated by '\n':
//!        {"room":"XXXX","role":"host|client","channel":"control|video|audio"}
//!   2. The relay pairs the "host" and "client" connections for the same
//!      room+channel.
//!   3. Once both are present it sends each side a single zero-length frame
//!      (4 zero bytes) as the "paired" signal.
//!   4. After that, every byte is forwarded verbatim in both directions. The
//!      [4B big-endian length][data] framing is end-to-end; the relay treats
//!      the stream as an opaque pipe (lower latency, no per-frame parsing).
//!
//! Not yet ported from Python: the HTTP/WebSocket relay for the mobile gateway
//! (port 9951). Run the Python relay for mobile until that lands.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use socket2::{SockRef, TcpKeepalive};
use tokio::io::{copy_bidirectional, AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{oneshot, Mutex};
use tokio::time::timeout;

const MAX_HEADER: usize = 1024;
const HEADER_TIMEOUT: Duration = Duration::from_secs(10);
const PAIR_TIMEOUT: Duration = Duration::from_secs(60);
const ROLES: [&str; 2] = ["host", "client"];
const CHANNELS: [&str; 3] = ["control", "video", "audio"];

/// One room+channel pairing slot. A connecting peer parks its stream here while
/// waiting for the other side; `*_done` wakes the parked task once paired (or is
/// dropped to tear it down when replaced).
#[derive(Default)]
struct Pair {
    host: Option<TcpStream>,
    client: Option<TcpStream>,
    host_done: Option<oneshot::Sender<()>>,
    client_done: Option<oneshot::Sender<()>>,
}

type Rooms = Arc<Mutex<HashMap<String, HashMap<String, Pair>>>>;

enum Outcome {
    /// Both sides present in this task: forward host <-> client.
    Forward(TcpStream, TcpStream),
    /// This side arrived first: wait to be paired (or time out).
    Wait(oneshot::Receiver<()>),
}

#[tokio::main]
async fn main() {
    let (bind_host, port) = parse_args();
    let rooms: Rooms = Arc::new(Mutex::new(HashMap::new()));

    let listener = match TcpListener::bind((bind_host.as_str(), port)).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("[relay] failed to bind {bind_host}:{port}: {e}");
            std::process::exit(1);
        }
    };

    println!("╔══════════════════════════════════════════════════════╗");
    println!("║       GameStream Relay Server (Rust/tokio)          ║");
    println!("╠══════════════════════════════════════════════════════╣");
    println!("║  TCP (host/client) : {bind_host}:{port}");
    println!("╚══════════════════════════════════════════════════════╝");
    println!("\n  host.py   : --relay <ip>:{port} --room XXXX");
    println!("  client.py : --relay <ip>:{port} --room XXXX");
    println!("\n  Waiting for connections...\n");

    loop {
        let (stream, peer) = match listener.accept().await {
            Ok(v) => v,
            Err(e) => {
                eprintln!("[relay] accept error: {e}");
                continue;
            }
        };
        let rooms = rooms.clone();
        tokio::spawn(async move {
            if let Err(e) = handle(stream, peer, rooms).await {
                eprintln!("  [relay] {peer} error: {e}");
            }
        });
    }
}

fn parse_args() -> (String, u16) {
    let mut host = "0.0.0.0".to_string();
    let mut port: u16 = 9950;
    let args: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--host" if i + 1 < args.len() => {
                host = args[i + 1].clone();
                i += 2;
            }
            "--port" if i + 1 < args.len() => {
                port = args[i + 1].parse().unwrap_or(9950);
                i += 2;
            }
            // accepted for CLI compatibility with the Python relay; the HTTP/WS
            // mobile relay is not implemented in Rust yet.
            "--http-port" if i + 1 < args.len() => i += 2,
            _ => i += 1,
        }
    }
    (host, port)
}

fn set_keepalive(stream: &TcpStream) {
    let ka = TcpKeepalive::new()
        .with_time(Duration::from_secs(5))
        .with_interval(Duration::from_secs(2));
    let _ = SockRef::from(stream).set_tcp_keepalive(&ka);
}

/// Read the JSON header line one byte at a time so we never consume bytes that
/// belong to the forwarded stream. Returns None on EOF / oversize / timeout.
async fn read_header_line(stream: &mut TcpStream) -> Option<String> {
    let mut buf = Vec::with_capacity(128);
    let mut byte = [0u8; 1];
    loop {
        let n = match timeout(HEADER_TIMEOUT, stream.read(&mut byte)).await {
            Ok(Ok(n)) => n,
            _ => return None,
        };
        if n == 0 {
            return None;
        }
        if byte[0] == b'\n' {
            return Some(String::from_utf8_lossy(&buf).into_owned());
        }
        buf.push(byte[0]);
        if buf.len() > MAX_HEADER {
            return None;
        }
    }
}

async fn handle(mut stream: TcpStream, peer: SocketAddr, rooms: Rooms) -> std::io::Result<()> {
    let _ = stream.set_nodelay(true);
    set_keepalive(&stream);

    let line = match read_header_line(&mut stream).await {
        Some(l) => l,
        None => return Ok(()),
    };

    let header: serde_json::Value = match serde_json::from_str(line.trim()) {
        Ok(v) => v,
        Err(_) => {
            eprintln!("  [relay] {peer} bad JSON header");
            return Ok(());
        }
    };

    let room = header.get("room").and_then(|v| v.as_str()).unwrap_or("").trim().to_uppercase();
    let role = header.get("role").and_then(|v| v.as_str()).unwrap_or("").to_lowercase();
    let channel = header
        .get("channel")
        .and_then(|v| v.as_str())
        .unwrap_or("control")
        .to_lowercase();

    if room.is_empty() || !ROLES.contains(&role.as_str()) || !CHANNELS.contains(&channel.as_str()) {
        eprintln!("  [relay] {peer} invalid header: room={room:?} role={role:?} channel={channel:?}");
        return Ok(());
    }

    println!("  [relay] {peer} → room={room} role={role} channel={channel}");

    let outcome = register(&rooms, &room, &role, &channel, stream).await;

    match outcome {
        Outcome::Forward(mut a, mut b) => {
            // Signal both peers that they are paired (one zero-length frame).
            let sig = [0u8; 4];
            let _ = a.write_all(&sig).await;
            let _ = b.write_all(&sig).await;
            let _ = a.flush().await;
            let _ = b.flush().await;
            println!("  [relay] room={room}/{channel} paired — forwarding");
            let _ = copy_bidirectional(&mut a, &mut b).await;
            println!("  [relay] room={room}/{channel} closed");
        }
        Outcome::Wait(rx) => {
            // Parked in the slot; wake on pairing, else reclaim on timeout.
            if timeout(PAIR_TIMEOUT, rx).await.is_err() {
                let mut map = rooms.lock().await;
                if let Some(rm) = map.get_mut(&room) {
                    if let Some(p) = rm.get_mut(&channel) {
                        match role.as_str() {
                            "host" => {
                                p.host = None;
                                p.host_done = None;
                            }
                            _ => {
                                p.client = None;
                                p.client_done = None;
                            }
                        }
                    }
                    prune(&mut map, &room, &channel);
                }
                println!("  [relay] {peer} room={room}/{channel} timed out waiting for peer");
            }
            // If notified: our stream was taken by the forwarder — nothing to do.
        }
    }

    Ok(())
}

async fn register(
    rooms: &Rooms,
    room: &str,
    role: &str,
    channel: &str,
    stream: TcpStream,
) -> Outcome {
    let mut map = rooms.lock().await;
    let room_map = map.entry(room.to_string()).or_default();
    let pair = room_map.entry(channel.to_string()).or_default();

    let (tx, rx) = oneshot::channel();
    if role == "host" {
        // Replace any stale host (dropping its stream + waker closes it).
        pair.host = Some(stream);
        pair.host_done = Some(tx);
    } else {
        pair.client = Some(stream);
        pair.client_done = Some(tx);
    }

    if pair.host.is_some() && pair.client.is_some() {
        let h = pair.host.take().unwrap();
        let c = pair.client.take().unwrap();
        // Wake whichever side was parked first.
        if let Some(done) = pair.host_done.take() {
            let _ = done.send(());
        }
        if let Some(done) = pair.client_done.take() {
            let _ = done.send(());
        }
        prune(&mut map, room, channel);
        Outcome::Forward(h, c)
    } else {
        Outcome::Wait(rx)
    }
}

/// Remove a channel pairing if empty, and the room if it then has no channels.
fn prune(map: &mut HashMap<String, HashMap<String, Pair>>, room: &str, channel: &str) {
    if let Some(rm) = map.get_mut(room) {
        let empty = rm
            .get(channel)
            .map(|p| p.host.is_none() && p.client.is_none())
            .unwrap_or(false);
        if empty {
            rm.remove(channel);
        }
        if rm.is_empty() {
            map.remove(room);
        }
    }
}
