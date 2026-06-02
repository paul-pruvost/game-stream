//! Wire protocol shared with the Python side (shared/protocol.py).

use std::io::{self, Read, Write};

use serde_json::Value;

pub const MSG_HEADER_LEN: usize = 4;
/// Video chunk header: !IIII = frame_id, chunk_idx, total_chunks, chunk_size.
pub const CHUNK_HEADER_LEN: usize = 16;

// MsgType values (shared/protocol.py)
pub const MSG_HANDSHAKE: i64 = 0;
pub const MSG_CONFIG: i64 = 2;

/// Send one length-prefixed JSON control message.
pub fn send_message(w: &mut impl Write, msg: &Value) -> io::Result<()> {
    let payload = serde_json::to_vec(msg).map_err(io_err)?;
    w.write_all(&(payload.len() as u32).to_be_bytes())?;
    w.write_all(&payload)?;
    w.flush()
}

/// Receive one length-prefixed JSON control message.
pub fn recv_message(r: &mut impl Read) -> io::Result<Value> {
    let mut hdr = [0u8; MSG_HEADER_LEN];
    r.read_exact(&mut hdr)?;
    let n = u32::from_be_bytes(hdr) as usize;
    if n > 2_000_000 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "message too large"));
    }
    let mut buf = vec![0u8; n];
    r.read_exact(&mut buf)?;
    serde_json::from_slice(&buf).map_err(io_err)
}

/// Parsed UDP video chunk header.
pub struct ChunkHeader {
    pub frame_id: u32,
    pub chunk_idx: u32,
    pub total_chunks: u32,
    pub chunk_size: u32,
}

/// Parse a video chunk: returns (header, payload slice) or None if malformed.
pub fn parse_chunk(packet: &[u8]) -> Option<(ChunkHeader, &[u8])> {
    if packet.len() < CHUNK_HEADER_LEN {
        return None;
    }
    let be = |i: usize| u32::from_be_bytes(packet[i..i + 4].try_into().unwrap());
    let h = ChunkHeader {
        frame_id: be(0),
        chunk_idx: be(4),
        total_chunks: be(8),
        chunk_size: be(12),
    };
    let end = CHUNK_HEADER_LEN + h.chunk_size as usize;
    if end > packet.len() {
        return None;
    }
    Some((h, &packet[CHUNK_HEADER_LEN..end]))
}

fn io_err(e: serde_json::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, e)
}
