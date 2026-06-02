//! Control connection: TLS/TCP to the host, handshake, config.

use std::io;
use std::net::TcpStream;

use native_tls::TlsStream;
use serde_json::{json, Value};

use crate::protocol::{recv_message, send_message, MSG_CONFIG, MSG_HANDSHAKE};

pub struct Control {
    pub stream: TlsStream<TcpStream>,
    pub config: Value,
}

/// Connect to a host.py over TLS, perform the handshake, return the (kept-open)
/// stream and the received CONFIG. Accepts the host's self-signed certificate,
/// matching the Python client's trust-on-first-use behaviour.
pub fn connect(host: &str, port: u16, video_port: u16) -> io::Result<Control> {
    let connector = native_tls::TlsConnector::builder()
        .danger_accept_invalid_certs(true)
        .danger_accept_invalid_hostnames(true)
        .build()
        .map_err(other)?;

    let tcp = TcpStream::connect((host, port))?;
    tcp.set_nodelay(true).ok();
    let mut stream = connector.connect(host, tcp).map_err(other)?;

    send_message(
        &mut stream,
        &json!({
            "type": MSG_HANDSHAKE,
            "magic": "GSTR",
            "version": "2.0",
            "video_port": video_port,
            "audio_port": video_port + 1,
        }),
    )?;

    let config = recv_message(&mut stream)?;
    if config.get("type").and_then(|v| v.as_i64()) != Some(MSG_CONFIG) {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "expected CONFIG reply"));
    }
    Ok(Control { stream, config })
}

fn other<E: std::fmt::Display>(e: E) -> io::Error {
    io::Error::new(io::ErrorKind::Other, e.to_string())
}
