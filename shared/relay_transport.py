#!/usr/bin/env python3
"""
RelayChannel — socket-compatible wrapper over the GameStream TCP relay.

Mimics enough of the socket interface (sendall, send, sendto, recv, recvfrom,
settimeout, close) so that host.py and client.py can use relay.py instead of
direct UDP/TCP without changing their inner logic.

Two receive modes:
  - recv(n)     : byte-stream semantics (for control channel / TLS)
  - recvfrom(n) : datagram semantics — returns exactly one relay frame per call
                  (for video/audio, matching UDP recvfrom behavior)

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
import queue
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
    recv() uses an internal byte-buffer for stream semantics.
    recvfrom() uses a queue of complete frames for datagram semantics.
    """

    def __init__(self, relay_addr: str, room: str, role: str, channel: str):
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
        self._write_lock = threading.Lock()

        # ── Stream-mode buffer (for recv / control channel) ──────────
        self._stream_buf = bytearray()
        self._stream_lock = threading.Lock()

        # ── Datagram-mode queue (for recvfrom / video+audio) ─────────
        # Each put() is one complete relay frame body = one application packet.
        self._dgram_queue: queue.Queue[bytes] = queue.Queue()

        # Which mode this channel uses for reading.
        # Set automatically: first call to recv() or recvfrom() decides.
        self._read_mode: str | None = None   # "stream" or "dgram"
        self._reader_thread: threading.Thread | None = None

        # Stored relay address for recvfrom()
        self._peer = (relay_addr, 0)

        # Set once connect() succeeds
        self._connected_event = threading.Event()
        self._connect_error: Exception | None = None

        # Set when the connection drops
        self._dead_event = threading.Event()

    @property
    def is_connected(self) -> bool:
        """True if the channel has a live socket and hasn't been marked dead."""
        return self._sock is not None and not self._dead_event.is_set()

    # ── Connection ────────────────────────────────────────────────────

    def connect(self):
        """
        Open TCP connection to relay, send JSON handshake header,
        then block until the relay sends the pairing signal (empty frame).
        """
        with self._write_lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

        self._stream_buf = bytearray()
        # Drain any old frames from the queue
        while not self._dgram_queue.empty():
            try:
                self._dgram_queue.get_nowait()
            except queue.Empty:
                break
        self._connected_event.clear()
        self._connect_error = None
        self._dead_event.clear()
        self._read_mode = None

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15.0)
            sock.connect((self._relay_host, self._relay_port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            header_line = json.dumps({
                "room":    self._room,
                "role":    self._role,
                "channel": self._channel,
            }).encode() + b"\n"
            sock.sendall(header_line)

            # Wait for the pairing signal: one empty frame (4 zero bytes)
            deadline = time.monotonic() + 60.0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    sock.close()
                    raise TimeoutError(
                        f"[relay] Timed out waiting for pairing on "
                        f"room={self._room}/{self._channel}"
                    )
                sock.settimeout(min(remaining, 5.0))
                raw = self._recvexactly_raw(sock, 4)
                if raw is None:
                    sock.close()
                    raise ConnectionResetError("[relay] EOF waiting for pairing")
                (length,) = _FRAME_HDR.unpack(raw)
                if length == 0:
                    break  # paired!
                body = self._recvexactly_raw(sock, length)
                if body is None:
                    sock.close()
                    raise ConnectionResetError("[relay] EOF reading pre-pairing frame")
                # Buffer any early data
                self._stream_buf.extend(body)

            sock.settimeout(self._timeout)
            with self._write_lock:
                self._sock = sock

            self._connect_error = None
            self._connected_event.set()

        except Exception as exc:
            self._connect_error = exc
            self._connected_event.set()
            self._dead_event.set()
            raise

    # ── Low-level raw socket read (no locks, used during connect) ─────

    @staticmethod
    def _recvexactly_raw(sock: socket.socket, n: int) -> bytes | None:
        """Read exactly n bytes from a raw socket. Returns None on EOF."""
        chunks = []
        received = 0
        while received < n:
            try:
                chunk = sock.recv(n - received)
            except socket.timeout:
                raise
            if not chunk:
                return None
            chunks.append(chunk)
            received += len(chunk)
        return b"".join(chunks)

    # ── Background reader thread (for datagram mode) ─────────────────

    def _start_reader_if_needed(self):
        """Start the background frame reader thread for datagram mode."""
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return
        self._reader_thread = threading.Thread(
            target=self._reader_loop, daemon=True, name=f"relay-reader-{self._channel}"
        )
        self._reader_thread.start()

    def _reader_loop(self):
        """Read relay frames and put them on the datagram queue."""
        try:
            while not self._dead_event.is_set():
                sock = self._sock
                if sock is None:
                    break
                try:
                    raw_hdr = self._recvexactly_raw(sock, 4)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if raw_hdr is None:
                    break
                (length,) = _FRAME_HDR.unpack(raw_hdr)
                if length == 0:
                    continue  # keepalive
                if length > _MAX_FRAME:
                    break
                try:
                    body = self._recvexactly_raw(sock, length)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if body is None:
                    break
                self._dgram_queue.put(body)
        except Exception:
            pass
        finally:
            self._dead_event.set()
            # Unblock any waiting recvfrom() with a sentinel
            self._dgram_queue.put(None)

    # ── Timeout ───────────────────────────────────────────────────────

    def settimeout(self, t):
        self._timeout = t
        if self._sock is not None:
            try:
                self._sock.settimeout(t)
            except OSError:
                pass

    # ── Sending ───────────────────────────────────────────────────────

    def _write_frame(self, data: bytes):
        """Write one length-prefixed frame to the relay socket."""
        frame = _FRAME_HDR.pack(len(data)) + data
        with self._write_lock:
            sock = self._sock
            if sock is None:
                self._dead_event.set()
                raise ConnectionResetError("[relay] not connected")
            total = 0
            try:
                while total < len(frame):
                    n = sock.send(frame[total:])
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

    # ── Receiving: stream mode (for recv / control channel) ──────────

    def _fill_stream_buffer(self, minimum: int):
        """Read frames into the stream buffer until it has >= minimum bytes."""
        sock = self._sock
        if sock is None:
            self._dead_event.set()
            raise ConnectionResetError("[relay] not connected")
        while len(self._stream_buf) < minimum:
            raw_hdr = self._recvexactly_raw(sock, 4)
            if raw_hdr is None:
                self._dead_event.set()
                raise ConnectionResetError("[relay] EOF reading frame header")
            (length,) = _FRAME_HDR.unpack(raw_hdr)
            if length == 0:
                continue
            if length > _MAX_FRAME:
                self._dead_event.set()
                raise ConnectionResetError(f"[relay] Frame too large: {length}")
            body = self._recvexactly_raw(sock, length)
            if body is None:
                self._dead_event.set()
                raise ConnectionResetError("[relay] EOF reading frame body")
            self._stream_buf.extend(body)

    def recv(self, n: int) -> bytes:
        """
        Byte-stream semantics: return up to n bytes.
        Used by the control channel (recv_message / TLS handshake).
        """
        self._read_mode = "stream"
        with self._stream_lock:
            if len(self._stream_buf) < 1:
                self._fill_stream_buffer(1)
            chunk = bytes(self._stream_buf[:n])
            del self._stream_buf[:n]
            return chunk

    # ── Receiving: datagram mode (for recvfrom / video+audio) ────────

    def recvfrom(self, n: int):
        """
        Datagram semantics: return exactly one complete relay frame per call,
        matching UDP recvfrom() behavior expected by VideoReceiver / AudioReceiver.
        The n parameter is ignored — the full frame is always returned.

        Blocks until connect() has succeeded.
        """
        # Wait for connection
        if not self._connected_event.is_set():
            self._connected_event.wait()
        if self._connect_error is not None:
            raise self._connect_error

        # Start the background reader on first call
        if self._read_mode != "dgram":
            self._read_mode = "dgram"
            self._start_reader_if_needed()

        # Block until a frame arrives (or connection dies)
        try:
            if self._timeout is not None:
                frame = self._dgram_queue.get(timeout=self._timeout)
            else:
                frame = self._dgram_queue.get(timeout=1.0)
                # If we got nothing but we're still alive, keep trying
                while frame is None and not self._dead_event.is_set():
                    frame = self._dgram_queue.get(timeout=1.0)
        except queue.Empty:
            raise socket.timeout("recvfrom timed out")

        if frame is None:
            # Sentinel from reader thread — connection is dead
            raise ConnectionResetError("[relay] connection closed")

        return frame, self._peer

    # ── Lifecycle ─────────────────────────────────────────────────────

    def wait_until_dead(self):
        """Block until the connection drops."""
        self._dead_event.wait()

    def close(self):
        with self._write_lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None
        self._dead_event.set()
        # Unblock any waiting recvfrom()
        self._dgram_queue.put(None)
