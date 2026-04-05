"""
GameStream Protocol v2 — Enhanced with H.264, Audio, and Encryption support.

Architecture:
  TCP Control Channel (TLS) : Input events, config, handshake, key exchange
  UDP Video   Channel (AES) : H.264 NAL units / keyframes
  UDP Audio   Channel (AES) : Opus-encoded audio packets

Frame format (UDP video):
  [12B nonce][4B frame_id][4B chunk_idx][4B total_chunks][4B chunk_size][encrypted payload][16B tag]

Audio format (UDP audio):
  [12B nonce][4B sequence][4B timestamp_ms][encrypted payload][16B tag]

Control messages (TCP/TLS):
  [4B payload_size][JSON payload]
"""

import struct
import json
import socket
import time
from enum import IntEnum
from typing import Optional

# ── Ports ──────────────────────────────────────────────────────────────
CONTROL_PORT = 9900
VIDEO_PORT   = 9901
AUDIO_PORT   = 9902

# ── Constants ──────────────────────────────────────────────────────────
MAX_UDP_PACKET   = 1400
FRAME_HEADER_FMT = "!IIII"        # frame_id, chunk_idx, total_chunks, chunk_size
FRAME_HEADER_LEN = struct.calcsize(FRAME_HEADER_FMT)
AUDIO_HEADER_FMT = "!II"          # sequence, timestamp_ms
AUDIO_HEADER_LEN = struct.calcsize(AUDIO_HEADER_FMT)
MSG_HEADER_FMT   = "!I"
MSG_HEADER_LEN   = struct.calcsize(MSG_HEADER_FMT)
MAGIC            = b"GSTR"
VERSION          = "2.0"

# ── Crypto ─────────────────────────────────────────────────────────────
NONCE_LEN        = 12             # AES-GCM nonce
TAG_LEN          = 16             # AES-GCM auth tag
AES_KEY_LEN      = 32            # AES-256

# ── Input Event Types ──────────────────────────────────────────────────
class InputType(IntEnum):
    KEY_DOWN       = 1
    KEY_UP         = 2
    MOUSE_MOVE     = 10
    MOUSE_BUTTON   = 11
    MOUSE_SCROLL   = 12
    GAMEPAD_AXIS   = 20
    GAMEPAD_BUTTON = 21
    GAMEPAD_HAT    = 22

class MsgType(IntEnum):
    HANDSHAKE      = 0
    INPUT_EVENT    = 1
    CONFIG         = 2
    PING           = 3
    PONG           = 4
    KEY_EXCHANGE   = 5    # Symmetric key for UDP encryption
    AUDIO_CONFIG   = 6    # Audio stream parameters
    QUALITY_ADJUST = 7    # Adaptive bitrate request
    CLIPBOARD      = 10   # Clipboard sync (host <-> client)
    FORCE_KEYFRAME = 11   # Client requests an IDR keyframe immediately
    DISCONNECT     = 99

# ── Codec types ────────────────────────────────────────────────────────
class VideoCodec(IntEnum):
    MJPEG   = 0   # Fallback
    H264    = 1
    H265    = 2

class AudioCodec(IntEnum):
    PCM     = 0   # Raw PCM (fallback)
    OPUS    = 1
    AAC     = 2


# ── Message helpers ────────────────────────────────────────────────────
def pack_message(msg_type: MsgType, data: dict) -> bytes:
    payload = json.dumps({"type": int(msg_type), **data}).encode("utf-8")
    return struct.pack(MSG_HEADER_FMT, len(payload)) + payload

def unpack_message(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))

def recv_message(sock) -> Optional[dict]:
    """Receive one control message from a TCP/TLS socket."""
    header = _recv_exact(sock, MSG_HEADER_LEN)
    if not header:
        return None
    (size,) = struct.unpack(MSG_HEADER_FMT, header)
    if size > 2_000_000:
        return None
    payload = _recv_exact(sock, size)
    if not payload:
        return None
    return unpack_message(payload)

def _recv_exact(sock, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (ConnectionResetError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ── Video frame chunking ──────────────────────────────────────────────
def chunk_frame(frame_id: int, data: bytes) -> list[bytes]:
    max_payload = MAX_UDP_PACKET - FRAME_HEADER_LEN
    total = max(1, (len(data) + max_payload - 1) // max_payload)
    packets = []
    for i in range(total):
        chunk = data[i * max_payload : (i + 1) * max_payload]
        header = struct.pack(FRAME_HEADER_FMT, frame_id, i, total, len(chunk))
        packets.append(header + chunk)
    return packets

def parse_chunk_header(packet: bytes):
    if len(packet) < FRAME_HEADER_LEN:
        return None
    fid, idx, total, size = struct.unpack(FRAME_HEADER_FMT, packet[:FRAME_HEADER_LEN])
    payload = packet[FRAME_HEADER_LEN : FRAME_HEADER_LEN + size]
    return fid, idx, total, size, payload

# ── Audio packet helpers ───────────────────────────────────────────────
def pack_audio(sequence: int, timestamp_ms: int, data: bytes) -> bytes:
    header = struct.pack(AUDIO_HEADER_FMT, sequence, timestamp_ms)
    return header + data

def unpack_audio(packet: bytes):
    if len(packet) < AUDIO_HEADER_LEN:
        return None
    seq, ts = struct.unpack(AUDIO_HEADER_FMT, packet[:AUDIO_HEADER_LEN])
    payload = packet[AUDIO_HEADER_LEN:]
    return seq, ts, payload


# ── Input event constructors ───────────────────────────────────────────
def make_input(input_type: InputType, **kwargs) -> bytes:
    return pack_message(MsgType.INPUT_EVENT, {
        "input_type": int(input_type),
        "timestamp": time.time(),
        **kwargs
    })

def make_key_event(key: str, pressed: bool) -> bytes:
    return make_input(InputType.KEY_DOWN if pressed else InputType.KEY_UP, key=key)

def make_mouse_move(x: float, y: float, relative: bool = True) -> bytes:
    return make_input(InputType.MOUSE_MOVE, x=x, y=y, relative=relative)

def make_mouse_button(button: int, pressed: bool) -> bytes:
    return make_input(InputType.MOUSE_BUTTON, button=button, pressed=pressed)

def make_mouse_scroll(dx: int, dy: int) -> bytes:
    return make_input(InputType.MOUSE_SCROLL, dx=dx, dy=dy)

def make_gamepad_axis(axis: int, value: float) -> bytes:
    return make_input(InputType.GAMEPAD_AXIS, axis=axis, value=value)

def make_gamepad_button(button: int, pressed: bool) -> bytes:
    return make_input(InputType.GAMEPAD_BUTTON, button=button, pressed=pressed)

def make_gamepad_hat(hat: int, value: tuple) -> bytes:
    return make_input(InputType.GAMEPAD_HAT, hat=hat, value=value)
