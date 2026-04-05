#!/usr/bin/env python3
"""
RelayChannel — socket-compatible wrapper over the GameStream TCP relay.

Mimics enough of the socket interface (sendall, send, sendto, recv, recvfrom,
settimeout, close) so that host.py and client.py can use relay.py instead of
direct UDP/TCP without changing their inner logic.

Framing (identical to relay.py):
    [4B big-endian length][data]

Usage:
    ch = RelayChannel("relay.example.com:9950", "ABCD", "host", "control")
    ch.connect()   # TCP + JSON handshake + waits for pairing signal
    ch.sendall(data)
    data = ch.recv(4096)
    ch.close()
"""

import json
import socket
import struct
import threading
import time

_FRAME_HDR = struct.Struct("!I")
_MAX_FRAME  = 16 * 1024 * 1024   # 16 MB — same limit as relay.py


class RelayChannel:
    """
    A socket-compatible channel over the GameStream relay TCP connection.

    Thread-safety: sendall/send/sendto are protected by a write lock.
    recv/recvfrom use an internal byte-buffer and a read lock so that
    callers can do recv(4) followed by recv(n) safely.
    """

    def __init__(self, relay_addr: str, room: str, role: str, channel: str):
        """
        relay_addr : "host:port" string
        room       : 4-char room code (will be uppercased)
        role       : "host" or "client"
        channel    : "control", "video", or "audio"
        """
        if ":" not in relay_addr:
            raise ValueError(f"relay_addr must be 'host:port', got {relay_addr!r}")
        host_part, port_part = relay_addr.rsplit(":", 1)
        self._relay_host = host_part
        self._relay_port = int(port_part)
        self._room    = room.strip().upper()
        self._role    = role
        self._channel = channel

        self._sock: socket.socket | None = None
        self._timeout: float | None = None

        # Internal recv buffer
        self._buf = bytearray()
        self._buf_lock = threading.Lock()
        self._write_lock = threading.Lock()

        # Stored relay address for recvfrom()
        self._peer = (relay_addr, 0)

        # Set once connect() succeeds; recvfrom() blocks on this so the
        # recv loop can be started before the relay handshake completes.
        self._connected_event = threading.Event()
        self._connect_error: Exception | None = None

        # Set when the connection drops (send/recv error).
        # wait_until_dead() blocks on this so _relay_channel_connector can
        # detect disconnection without polling.
        self._dead_event = threading.Event()

    # ── Connection ────────────────────────────────────────────────────

    def connect(self):
        """
        Open TCP connection to relay, send JSON handshake header,
        then block until the relay sends the pairing signal (empty frame).
        Raises ConnectionRefusedError / OSError on failure.
        """
        # Close previous socket if reconnecting
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._buf = bytearray()
        self._connected_event.clear()
        self._connect_error = None
        self._dead_event.clear()

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(15.0)
            self._sock.connect((self._relay_host, self._relay_port))
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            header_line = json.dumps({
                "room":    self._room,
                "role":    self._role,
                "channel": self._channel,
            }).encode() + b"\n"
            self._sock.sendall(header_line)

            # Wait for the pairing signal: one empty frame (4 zero bytes)
            # relay.py sends FRAME_HEADER.pack(0) to both sides when paired.
            deadline = time.monotonic() + 60.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._sock.close()
                    raise TimeoutError(
                        f"[relay] Timed out waiting for pairing on "
                        f"room={self._room}/{self._channel}"
                    )
                self._sock.settimeout(min(remaining, 5.0))
                raw = self._recvexactly(4)
                if raw is None:
                    self._sock.close()
                    raise ConnectionResetError("[relay] EOF while waiting for pairing signal")
                (length,) = _FRAME_HDR.unpack(raw)
                if length == 0:
                    # Pairing signal received — we're live
                    break
                # Non-empty frame before pairing is unexpected; buffer it anyway
                body = self._recvexactly(length)
                if body is None:
                    self._sock.close()
                    raise ConnectionResetError("[relay] EOF reading pre-pairing frame body")
                with self._buf_lock:
                    self._buf.extend(body)

            # Restore user-set timeout (or blocking)
            self._sock.settimeout(self._timeout)

            # Signal waiting recvfrom() / recv() calls that we are live
            self._connect_error = None
            self._connected_event.set()

        except Exception as exc:
            # Unblock any recvfrom() already waiting on _connected_event
            self._connect_error = exc
            self._connected_event.set()
            self._dead_event.set()   # unblock wait_until_dead() on error too
            raise

    # ── Timeout ───────────────────────────────────────────────────────

    def settimeout(self, t):
        self._timeout = t
        if self._sock is not None:
            self._sock.settimeout(t)

    # ── Sending ───────────────────────────────────────────────────────

    def _write_frame(self, data: bytes):
        """Write one length-prefixed frame to the relay socket."""
        frame = _FRAME_HDR.pack(len(data)) + data
        with self._write_lock:
            if self._sock is None:
                self._dead_event.set()
                raise ConnectionResetError("[relay] not connected")
            total = 0
            try:
                while total < len(frame):
                    n = self._sock.send(frame[total:])
                    if n == 0:
                        self._dead_event.set()
                        raise ConnectionResetError("[relay] send returned 0")
                    total += n
            except OSError:
                self._dead_event.set()
                raise

    def sendall(self, data: bytes):
        self._write_frame(data)

    def send(self, data: bytes) -> int:
        self._write_frame(data)
        return len(data)

    def sendto(self, data: bytes, addr):
        """addr is ignored — all data goes through the relay."""
        self._write_frame(data)
        return len(data)

    # ── Receiving ────────────────────────────────────────────────────

    def _recvexactly(self, n: int) -> bytes | None:
        """
        Read exactly n bytes from the raw socket.
        Returns None on EOF/error.
        Respects the current socket timeout (raises socket.timeout if needed).
        """
        chunks = []
        received = 0
        while received < n:
            try:
                chunk = self._sock.recv(n - received)
            except socket.timeout:
                raise  # propagate as-is so callers see socket.timeout
            if not chunk:
                return None
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    def _fill_buffer(self, minimum: int):
        """
        Read frames from the relay until the internal buffer has at least
        `minimum` bytes available.
        Raises socket.timeout or ConnectionResetError as appropriate.
        """
        while len(self._buf) < minimum:
            # Read frame header
            raw_hdr = self._recvexactly(4)
            if raw_hdr is None:
                self._dead_event.set()
                raise ConnectionResetError("[relay] EOF reading frame header")
            (length,) = _FRAME_HDR.unpack(raw_hdr)
            if length == 0:
                # Another pairing / keepalive signal — ignore
                continue
            if length > _MAX_FRAME:
                self._dead_event.set()
                raise ConnectionResetError(
                    f"[relay] Frame too large: {length} bytes"
                )
            body = self._recvexactly(length)
            if body is None:
                self._dead_event.set()
                raise ConnectionResetError("[relay] EOF reading frame body")
            self._buf.extend(body)

    def recv(self, n: int) -> bytes:
        """
        Return up to n bytes from the internal buffer (or the relay).
        Raises socket.timeout if the underlying socket times out.
        Raises ConnectionResetError on EOF.
        """
        with self._buf_lock:
            if len(self._buf) < n:
                self._fill_buffer(1)  # fill at least one more frame
            # Return however much is available, up to n
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
            return chunk

    def recvfrom(self, n: int):
        """
        Return (data, relay_addr).
        Each call returns exactly one complete relay frame (the n limit is
        advisory — full frames are returned to match UDP semantics expected
        by VideoReceiver / AudioReceiver).

        Blocks until connect() has succeeded so that the recv loop can be
        started before the relay handshake completes (avoids AttributeError
        on a None socket that would silently kill the receiver thread).
        """
        # Wait for the channel to be connected (or an error to be set)
        if not self._connected_event.is_set():
            self._connected_event.wait()
        if self._connect_error is not None:
            raise self._connect_error

        with self._buf_lock:
            # We want a complete frame; fill until we have data
            self._fill_buffer(1)
            chunk = bytes(self._buf[:n])
            del self._buf[:n]
        return chunk, self._peer

    def wait_until_dead(self):
        """
        Block until the connection drops (send or recv error).
        Used by _relay_channel_connector to detect disconnection without polling.
        Returns immediately if already dead.
        """
        self._dead_event.wait()

    # ── Close ─────────────────────────────────────────────────────────

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._dead_event.set()   # unblock wait_until_dead() on explicit close
