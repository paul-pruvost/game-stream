#!/usr/bin/env python3
"""
GameStream Client v2 — H.264 + Audio + TLS Encryption.

Connects to Host via TLS, receives encrypted H.264 video + Opus audio,
displays fullscreen with pygame. All input forwarded to host.

Usage:
    python client.py <host_ip> [--port 9900] [--fullscreen] [--grab-mouse]
                     [--fingerprint AA:BB:...] [--no-audio] [--auto]
                     [--relay host:port] [--room XXXX]
"""

import argparse
import socket
import struct
import threading
import time
import sys
import os
import queue

try:
    import numpy as np
    import pygame
except ImportError as e:
    print(f"❌  Missing dependency: {e}")
    print("    pip install numpy pygame")
    sys.exit(1)

# Optional: pyperclip for clipboard sync
try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.protocol import (
    CONTROL_PORT, VIDEO_PORT, AUDIO_PORT, MsgType, InputType,
    pack_message, recv_message, parse_chunk_header, unpack_audio,
    MAGIC, VERSION,
    make_key_event, make_mouse_move, make_mouse_button,
    make_mouse_scroll, make_gamepad_axis, make_gamepad_button, make_gamepad_hat,
)
from shared.video_codec import create_decoder
from shared.crypto import (
    create_client_ssl_context, verify_server_cert,
    SessionCipher, decode_session_key,
)
from shared.relay_transport import RelayChannel
from shared.pairing import KnownHosts


class VideoReceiver:
    """Receives and reassembles encrypted H.264 video frames over UDP (or relay)."""

    def __init__(self, port: int, cipher=None, relay_channel: "RelayChannel | None" = None):
        self.port = port
        self.cipher = cipher
        if relay_channel is not None:
            self.sock = relay_channel
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            self.sock.settimeout(0.05)
            self.sock.bind(("0.0.0.0", port))

        self._frames = {}
        self._latest_id = -1
        self._lock = threading.Lock()
        self.current_frame = None  # Decoded RGB numpy array
        self.frames_received = 0
        self.frames_dropped = 0
        self.running = True
        self.decoder = None       # Set after handshake
        self.last_frame_time = time.time()

    def start(self):
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except:
            pass
        if self.decoder:
            self.decoder.close()

    def _recv_loop(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                break

            parsed = parse_chunk_header(data)
            if not parsed:
                continue
            fid, idx, total, size, payload = parsed

            if fid < self._latest_id - 2:
                self.frames_dropped += 1
                continue

            with self._lock:
                if fid not in self._frames:
                    self._frames[fid] = {"chunks": {}, "total": total}
                self._frames[fid]["chunks"][idx] = payload

                if len(self._frames[fid]["chunks"]) == total:
                    frame_data = b"".join(
                        self._frames[fid]["chunks"][i] for i in range(total)
                    )

                    if self.cipher:
                        frame_data = self.cipher.decrypt(frame_data)
                        if frame_data is None:
                            self._latest_id = fid
                            for k in [k for k in self._frames if k <= fid]:
                                del self._frames[k]
                            continue

                    if self.decoder:
                        img = self.decoder.decode(frame_data)
                        if img is not None:
                            self.current_frame = img
                            self.frames_received += 1
                            self.last_frame_time = time.time()

                    self._latest_id = fid
                    for k in [k for k in self._frames if k <= fid]:
                        del self._frames[k]


class AudioReceiver:
    """Receives, decrypts, and decodes audio over UDP (or relay)."""

    def __init__(self, port: int, cipher=None, relay_channel: "RelayChannel | None" = None):
        self.port = port
        self.cipher = cipher
        if relay_channel is not None:
            self.sock = relay_channel
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            self.sock.settimeout(0.5)
            self.sock.bind(("0.0.0.0", port))
        self.running = True
        self.decoder = None
        self.player = None
        self.packets_received = 0

    def start(self):
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except:
            pass
        if self.player:
            self.player.stop()
        if self.decoder:
            self.decoder.close()

    def _recv_loop(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break

            if self.cipher:
                data = self.cipher.decrypt(data)
                if data is None:
                    continue

            parsed = unpack_audio(data)
            if not parsed:
                continue
            seq, ts, opus_data = parsed

            if self.decoder and self.player:
                pcm = self.decoder.decode(opus_data)
                if pcm is not None:
                    self.player.write(pcm)
                    self.packets_received += 1


class ControlConnection:
    """TLS-encrypted TCP connection to host for input and config (or relay channel)."""

    def __init__(self, host: str, port: int, video_port: int, audio_port: int,
                 use_encryption: bool = True, fingerprint: str = None,
                 relay_channel: "RelayChannel | None" = None):
        self.host = host
        self.port = port
        self.video_port = video_port
        self.audio_port = audio_port
        self.use_encryption = use_encryption
        self.expected_fingerprint = fingerprint
        self._relay_channel = relay_channel
        self.sock = None
        self.connected = False
        self.config = {}
        self._lock = threading.Lock()
        self.latency_ms = 0.0
        self.server_fingerprint = ""
        # RTT tracking for adaptive bitrate
        self.fingerprint_mismatch = False
        self._rtt_history = []
        self._rtt_lock = threading.Lock()
        self._max_bitrate = 8_000_000  # set from outside

    def connect(self) -> bool:
        try:
            if self._relay_channel is not None:
                # Relay mode: connect the relay channel (blocks until paired)
                print(f"  📡  Connecting via relay...")
                self._relay_channel.connect()
                self.sock = self._relay_channel
                print(f"  📡  Relay control channel paired")
            else:
                raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                raw.settimeout(5.0)
                raw.connect((self.host, self.port))
                raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                if self.use_encryption:
                    ssl_ctx = create_client_ssl_context(self.expected_fingerprint)
                    self.sock = ssl_ctx.wrap_socket(raw, server_hostname=self.host)

                    valid, fp = verify_server_cert(self.sock, self.expected_fingerprint)
                    self.server_fingerprint = fp

                    if self.expected_fingerprint and not valid:
                        self.fingerprint_mismatch = True
                        self.sock.close()
                        return False
                else:
                    self.sock = raw

            # Handshake
            hs = pack_message(MsgType.HANDSHAKE, {
                "magic": MAGIC.decode(),
                "version": VERSION,
                "video_port": self.video_port,
                "audio_port": self.audio_port,
            })
            self.sock.sendall(hs)

            self.sock.settimeout(5.0)
            msg = recv_message(self.sock)
            if msg and msg.get("type") == MsgType.CONFIG:
                self.config = msg
                self.connected = True
                return True
            return False
        except Exception as e:
            print(f"  ❌  Connection failed: {e}")
            return False

    def send(self, data: bytes):
        if not self.connected:
            return
        with self._lock:
            try:
                self.sock.sendall(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.connected = False

    def recv_loop(self, on_message):
        """Receive messages from host (runs in a thread)."""
        self.sock.settimeout(5.0)
        while self.connected:
            try:
                msg = recv_message(self.sock)
            except socket.timeout:
                continue
            if msg is None:
                self.connected = False
                break
            on_message(msg)

    def start_ping_loop(self):
        def _loop():
            while self.connected:
                t0 = time.time()
                self.send(pack_message(MsgType.PING, {"ts": t0}))
                time.sleep(2)
        threading.Thread(target=_loop, daemon=True).start()

    def record_rtt(self, rtt_ms: float):
        with self._rtt_lock:
            self._rtt_history.append(rtt_ms)
            if len(self._rtt_history) > 5:
                self._rtt_history.pop(0)

    def avg_rtt(self) -> float:
        with self._rtt_lock:
            if not self._rtt_history:
                return 0.0
            return sum(self._rtt_history) / len(self._rtt_history)

    def start_quality_loop(self):
        """Adaptive bitrate: adjust quality every 5s based on RTT."""
        current_bitrate = [self._max_bitrate]

        def _loop():
            while self.connected:
                time.sleep(5)
                if not self._rtt_history:
                    continue
                rtt = self.avg_rtt()
                br = current_bitrate[0]
                if rtt > 150:
                    new_br = max(1_000_000, int(br * 0.8))
                elif rtt < 50:
                    new_br = min(self._max_bitrate, int(br * 1.1))
                else:
                    continue

                if new_br != br:
                    current_bitrate[0] = new_br
                    self.send(pack_message(MsgType.QUALITY_ADJUST, {"bitrate": new_br}))
                    print(f"  📊  Adaptive bitrate: {new_br // 1_000_000:.1f} Mbps (RTT={rtt:.0f}ms)")

        threading.Thread(target=_loop, daemon=True).start()

    def disconnect(self):
        self.send(pack_message(MsgType.DISCONNECT, {}))
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass


class GameStreamClient:
    def __init__(self, args):
        self.args = args
        self.running = False
        self.fullscreen = args.fullscreen
        self.grab_mouse = args.grab_mouse
        self.show_stats = True
        self._reconnect_message = None
        self.known_hosts = KnownHosts()

    def _resolve_host(self) -> str:
        """Return host IP, auto-discovering via mDNS if --auto is set."""
        host = getattr(self.args, "host", None)
        if host and host != "auto":
            return host

        print("  🔍  Auto-discovering GameStream hosts via mDNS...")
        try:
            from shared.discovery import ServiceDiscovery, HAS_ZEROCONF
            if not HAS_ZEROCONF:
                print("  ❌  zeroconf not installed; cannot auto-discover")
                sys.exit(1)

            found = []
            event = threading.Event()

            def on_found(name, ip, port):
                print(f"  ✅  Found: {name} at {ip}:{port}")
                found.append((name, ip, port))
                event.set()

            disc = ServiceDiscovery(on_found)
            disc.start()
            event.wait(timeout=5.0)
            disc.stop()

            if not found:
                print("  ❌  No GameStream hosts found on LAN")
                sys.exit(1)

            name, ip, port = found[0]
            print(f"  📡  Using: {name} @ {ip}:{port}")
            self.args.port = port
            return ip

        except Exception as e:
            print(f"  ❌  mDNS discovery error: {e}")
            sys.exit(1)

    def start(self):
        relay_addr = getattr(self.args, "relay", None)
        relay_room = (getattr(self.args, "room", None) or "").strip().upper()
        relay_mode = bool(relay_addr)

        host_ip = self._resolve_host() if not relay_mode else relay_addr
        use_enc = (not self.args.no_encryption) and (not relay_mode)
        max_bitrate = getattr(self.args, "bitrate", 8_000_000)

        # Pairing: resolve fingerprint
        explicit_fp = self.args.fingerprint
        pin_fp = explicit_fp
        if use_enc and not relay_mode and not explicit_fp:
            saved_fp = self.known_hosts.fingerprint(host_ip, self.args.port)
            if getattr(self.args, 'trust_new', False):
                pin_fp = None  # Accept any cert, will update pairing after
            elif saved_fp:
                pin_fp = saved_fp
            else:
                pin_fp = None  # First time: trust-on-first-use

        if relay_mode:
            print(f"\n╔══════════════════════════════════════════════════════╗")
            print(f"║         GameStream Client  v2.0  [RELAY]            ║")
            print(f"╠══════════════════════════════════════════════════════╣")
            print(f"║  Relay : {relay_addr:<43}║")
            print(f"║  Room  : {relay_room:<43}║")
            print(f"╚══════════════════════════════════════════════════════╝")
        else:
            print(f"\n╔══════════════════════════════════════════════════════╗")
            print(f"║         🎮  GameStream Client  v2.0                 ║")
            print(f"╠══════════════════════════════════════════════════════╣")
            print(f"║  Host        : {host_ip:<37}║")
            print(f"║  Control port: {self.args.port:<37}║")
            enc = "TLS + AES-256-GCM" if use_enc else "DISABLED"
            print(f"║  Encryption  : {enc:<37}║")
            print(f"╚══════════════════════════════════════════════════════╝")

        # Pygame init (done once outside reconnect loop)
        pygame.init()
        pygame.display.set_caption(f"GameStream — {host_ip}")
        pygame.joystick.init()
        self.joysticks = {}
        self._init_joysticks()

        if self.fullscreen:
            info = pygame.display.Info()
            self.win_w, self.win_h = info.current_w, info.current_h
            self.screen = pygame.display.set_mode(
                (self.win_w, self.win_h),
                pygame.FULLSCREEN | pygame.HWSURFACE | pygame.DOUBLEBUF
            )
        else:
            self.win_w, self.win_h = 1280, 720
            self.screen = pygame.display.set_mode(
                (self.win_w, self.win_h),
                pygame.RESIZABLE | pygame.HWSURFACE | pygame.DOUBLEBUF
            )

        if self.grab_mouse:
            pygame.event.set_grab(True)
            pygame.mouse.set_visible(False)

        self.clock = pygame.time.Clock()
        self.running = True

        while self.running:
            # Auto-reconnect loop
            if relay_mode:
                relay_ctrl_ch  = RelayChannel(relay_addr, relay_room, "client", "control")
                relay_video_ch = RelayChannel(relay_addr, relay_room, "client", "video")
                relay_audio_ch = RelayChannel(relay_addr, relay_room, "client", "audio")
                self.ctrl = ControlConnection(
                    relay_addr, self.args.port,
                    self.args.video_port, self.args.audio_port,
                    use_encryption=False,
                    fingerprint=None,
                    relay_channel=relay_ctrl_ch,
                )
            else:
                relay_video_ch = None
                relay_audio_ch = None
                self.ctrl = ControlConnection(
                    host_ip, self.args.port,
                    self.args.video_port, self.args.audio_port,
                    use_encryption=use_enc,
                    fingerprint=pin_fp,
                )
            self.ctrl._max_bitrate = max_bitrate

            if not self.ctrl.connect():
                if not self.running:
                    break
                # Pairing: certificate changed since last connection
                if self.ctrl.fingerprint_mismatch and not explicit_fp:
                    new_fp = self.ctrl.server_fingerprint
                    print(f"\n  ⚠️  HOST CERTIFICATE HAS CHANGED!")
                    print(f"      Saved : {pin_fp}")
                    print(f"      Got   : {new_fp}")
                    print(f"\n      The host may have regenerated its certificate.")
                    print(f"      To accept: python client.py {host_ip} --trust-new")
                    print(f"      To forget: python client.py {host_ip} --forget")
                    self.running = False
                    break
                self._reconnect_message = "Could not connect. Retrying in 3s..."
                self._show_reconnect_screen()
                time.sleep(3)
                continue

            cfg = self.ctrl.config
            host_w = cfg.get("width", 1920)
            host_h = cfg.get("height", 1080)
            codec = cfg.get("codec", "mjpeg")
            audio_enabled = cfg.get("audio_enabled", False) and not self.args.no_audio
            encrypted = cfg.get("encrypted", False)

            # Pairing: save or verify fingerprint
            if use_enc and not relay_mode and not explicit_fp:
                fp = self.ctrl.server_fingerprint
                hostname = cfg.get("hostname", "")
                saved = self.known_hosts.fingerprint(host_ip, self.args.port)
                if saved and fp == saved:
                    print(f"  🔗  Paired: {hostname or host_ip}")
                else:
                    self.known_hosts.save(host_ip, self.args.port, fp, hostname)
                    if saved:
                        print(f"  🔗  Pairing updated: {hostname or host_ip}")
                    else:
                        print(f"  🔗  Device paired: {hostname or host_ip}")

            print(f"  ✅  Connected! {host_w}x{host_h} @ {codec}")
            print(f"  ⌨️   F11=Fullscreen  F10=Mouse grab  F9=Stats  Ctrl+Shift+Q=Quit\n")
            self._reconnect_message = None

            # Session cipher for UDP (not used in relay mode)
            cipher = None
            if encrypted and "session_key" in cfg and not relay_mode:
                key = decode_session_key(cfg["session_key"])
                cipher = SessionCipher(key)
                print(f"  🔒  UDP streams encrypted (AES-256-GCM)")

            # Video receiver + decoder
            if relay_mode:
                # Connect relay video channel in background
                def _connect_relay_video():
                    try:
                        relay_video_ch.connect()
                        print(f"  📡  Relay video channel ready")
                    except Exception as e:
                        print(f"  ⚠️  Relay video connect failed: {e}")
                threading.Thread(target=_connect_relay_video, daemon=True).start()
                self.video = VideoReceiver(self.args.video_port, cipher=cipher,
                                           relay_channel=relay_video_ch)
            else:
                self.video = VideoReceiver(self.args.video_port, cipher=cipher)
            self.video.decoder = create_decoder(codec)
            self.video.start()
            self.host_w, self.host_h = host_w, host_h

            # Audio receiver
            self.audio = None
            if audio_enabled:
                try:
                    from shared.audio_stream import create_audio_decoder, AudioPlayer
                    if relay_mode:
                        def _connect_relay_audio():
                            try:
                                relay_audio_ch.connect()
                                print(f"  📡  Relay audio channel ready")
                            except Exception as e:
                                print(f"  ⚠️  Relay audio connect failed: {e}")
                        threading.Thread(target=_connect_relay_audio, daemon=True).start()
                        self.audio = AudioReceiver(self.args.audio_port, cipher=cipher,
                                                   relay_channel=relay_audio_ch)
                    else:
                        self.audio = AudioReceiver(self.args.audio_port, cipher=cipher)
                    self.audio.decoder = create_audio_decoder()
                    self.audio.player = AudioPlayer()
                    self.audio.player.start()
                    self.audio.start()
                    print(f"  🔈  Audio stream active")
                except Exception as e:
                    print(f"  ⚠️  Audio init failed: {e}")
                    self.audio = None

            # Start control message receive loop (for PONG, CLIPBOARD)
            threading.Thread(target=self.ctrl.recv_loop,
                             args=(self._on_control_message,),
                             daemon=True).start()

            # Ping + quality adaptation
            self.ctrl.start_ping_loop()
            self.ctrl.start_quality_loop()

            # Run main loop until disconnected or user quits
            self._session_loop()

            # Cleanup after disconnect
            self.video.stop()
            if self.audio:
                self.audio.stop()
            try:
                self.ctrl.disconnect()
            except Exception:
                pass

            if not self.running:
                break

            # Show reconnect screen and wait
            self._reconnect_message = "Reconnecting..."
            print("\n  🔄  Connection lost. Reconnecting in 3s...")
            time.sleep(3)

        pygame.quit()
        print("\n👋  Client shut down.")

    def _on_control_message(self, msg: dict):
        """Handle messages received from the host (e.g. PONG, CLIPBOARD)."""
        mt = msg.get("type")
        if mt == MsgType.PONG:
            ts = msg.get("ts", 0)
            rtt_ms = (time.time() - ts) * 1000
            self.ctrl.record_rtt(rtt_ms)
            self.ctrl.latency_ms = rtt_ms
        elif mt == MsgType.CLIPBOARD:
            if HAS_CLIPBOARD:
                text = msg.get("text", "")
                try:
                    pyperclip.copy(text)
                    print(f"  📋  Clipboard received from host ({len(text)} chars)")
                except Exception as e:
                    print(f"  ⚠️  Clipboard write failed: {e}")

    def _show_reconnect_screen(self):
        """Draw a simple "reconnecting" message while not in session."""
        if not self.running:
            return
        font = pygame.font.SysFont("monospace", 20)
        self.screen.fill((20, 20, 30))
        msg_text = self._reconnect_message or "Reconnecting..."
        surf = font.render(msg_text, True, (200, 200, 100))
        self.screen.blit(surf, (self.win_w // 2 - surf.get_width() // 2,
                                self.win_h // 2))
        pygame.display.flip()

    def _session_loop(self):
        """Run the main rendering loop for one connected session."""
        font = pygame.font.SysFont("monospace", 16)
        fps_display = 0
        fps_timer = time.time()
        fc = 0

        while self.running and self.ctrl.connected:
            for ev in pygame.event.get():
                self._handle_event(ev)

            if self.grab_mouse and pygame.event.get_grab():
                rel = pygame.mouse.get_rel()
                if rel != (0, 0):
                    self.ctrl.send(make_mouse_move(rel[0], rel[1], relative=True))

            frame = self.video.current_frame
            if frame is not None:
                h, w = frame.shape[:2]
                surf = pygame.image.frombuffer(frame, (w, h), 'RGB').convert()
                if w == self.win_w and h == self.win_h:
                    self.screen.blit(surf, (0, 0))
                else:
                    pygame.transform.scale(surf, (self.win_w, self.win_h), self.screen)
                fc += 1
            else:
                self.screen.fill((20, 20, 30))
                msg = font.render("Waiting for video stream...", True, (180, 180, 200))
                self.screen.blit(msg, (self.win_w // 2 - msg.get_width() // 2, self.win_h // 2))

            now = time.time()
            if now - fps_timer >= 1.0:
                fps_display = fc / (now - fps_timer)
                fc = 0
                fps_timer = now

            if self.show_stats:
                enc_icon = "🔒" if self.ctrl.config.get("encrypted") else "🔓"
                codec_name = self.ctrl.config.get("codec", "?")
                rtt = self.ctrl.latency_ms
                stats = [
                    f"FPS: {fps_display:.0f}  |  Codec: {codec_name}",
                    f"Decoded: {self.video.frames_received}  |  Dropped: {self.video.frames_dropped}",
                    f"Grab: {'ON' if pygame.event.get_grab() else 'OFF'}  |  "
                    f"Gamepads: {len(self.joysticks)}",
                    f"{enc_icon} {'Encrypted' if self.ctrl.config.get('encrypted') else 'Unencrypted'}"
                    + (f"  |  Audio pkts: {self.audio.packets_received}" if self.audio else ""),
                    f"RTT: {rtt:.0f} ms  |  Avg RTT: {self.ctrl.avg_rtt():.0f} ms",
                ]
                y = 8
                for s in stats:
                    txt = font.render(s, True, (0, 255, 100))
                    bg = pygame.Surface((txt.get_width() + 10, txt.get_height() + 4))
                    bg.set_alpha(180)
                    bg.fill((0, 0, 0))
                    self.screen.blit(bg, (6, y - 2))
                    self.screen.blit(txt, (10, y))
                    y += 22

            pygame.display.flip()
            self.clock.tick(144)

    def _init_joysticks(self):
        for i in range(pygame.joystick.get_count()):
            js = pygame.joystick.Joystick(i)
            js.init()
            self.joysticks[js.get_instance_id()] = js
            print(f"  🎮  Gamepad: {js.get_name()} ({js.get_numaxes()} axes, {js.get_numbuttons()} btn)")

    def _handle_event(self, event):
        if event.type == pygame.QUIT:
            self.running = False
            return

        if event.type == pygame.VIDEORESIZE:
            self.win_w, self.win_h = event.w, event.h

        # Keyboard
        if event.type in (pygame.KEYDOWN, pygame.KEYUP):
            pressed = event.type == pygame.KEYDOWN
            name = pygame.key.name(event.key)
            if pressed:
                mods = pygame.key.get_mods()
                if event.key == pygame.K_F11:
                    return self._toggle_fullscreen()
                if event.key == pygame.K_F10:
                    return self._toggle_grab()
                if event.key == pygame.K_F9:
                    self.show_stats = not self.show_stats
                    return
                if event.key == pygame.K_q and (mods & pygame.KMOD_CTRL) and (mods & pygame.KMOD_SHIFT):
                    self.running = False
                    return
                # Clipboard sync: Ctrl+C while mouse is grabbed
                if (event.key == pygame.K_c and (mods & pygame.KMOD_CTRL)
                        and pygame.event.get_grab() and HAS_CLIPBOARD):
                    try:
                        text = pyperclip.paste()
                        if text:
                            self.ctrl.send(pack_message(MsgType.CLIPBOARD, {
                                "direction": "push",
                                "text": text,
                            }))
                            print(f"  📋  Clipboard sent to host ({len(text)} chars)")
                    except Exception as e:
                        print(f"  ⚠️  Clipboard read failed: {e}")
                    # Still forward the key event
            self.ctrl.send(make_key_event(name, pressed))

        # Mouse buttons
        if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP):
            if event.button in (4, 5):
                self.ctrl.send(make_mouse_scroll(0, 1 if event.button == 4 else -1))
            else:
                self.ctrl.send(make_mouse_button(event.button, event.type == pygame.MOUSEBUTTONDOWN))

        # Mouse move (absolute)
        if event.type == pygame.MOUSEMOTION and not pygame.event.get_grab():
            self.ctrl.send(make_mouse_move(event.pos[0] / self.win_w, event.pos[1] / self.win_h, relative=False))

        # Scroll wheel
        if event.type == pygame.MOUSEWHEEL:
            self.ctrl.send(make_mouse_scroll(event.x, event.y))

        # Gamepad
        if event.type == pygame.JOYDEVICEADDED:
            js = pygame.joystick.Joystick(event.device_index)
            js.init()
            self.joysticks[js.get_instance_id()] = js
            print(f"  🎮  Connected: {js.get_name()}")

        if event.type == pygame.JOYDEVICEREMOVED:
            if event.instance_id in self.joysticks:
                del self.joysticks[event.instance_id]

        if event.type == pygame.JOYAXISMOTION:
            v = event.value if abs(event.value) >= 0.05 else 0.0
            self.ctrl.send(make_gamepad_axis(event.axis, v))

        if event.type in (pygame.JOYBUTTONDOWN, pygame.JOYBUTTONUP):
            self.ctrl.send(make_gamepad_button(event.button, event.type == pygame.JOYBUTTONDOWN))

        if event.type == pygame.JOYHATMOTION:
            self.ctrl.send(make_gamepad_hat(event.hat, event.value))

    def _toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            info = pygame.display.Info()
            self.win_w, self.win_h = info.current_w, info.current_h
            self.screen = pygame.display.set_mode(
                (self.win_w, self.win_h), pygame.FULLSCREEN | pygame.HWSURFACE | pygame.DOUBLEBUF
            )
        else:
            self.win_w, self.win_h = 1280, 720
            self.screen = pygame.display.set_mode(
                (self.win_w, self.win_h), pygame.RESIZABLE | pygame.HWSURFACE | pygame.DOUBLEBUF
            )

    def _toggle_grab(self):
        self.grab_mouse = not self.grab_mouse
        pygame.event.set_grab(self.grab_mouse)
        pygame.mouse.set_visible(not self.grab_mouse)


def main():
    p = argparse.ArgumentParser(description="GameStream Client v2")
    p.add_argument("host", nargs="?", default="auto",
                   help="Host IP address (omit or 'auto' to discover via mDNS)")
    p.add_argument("--auto", action="store_true", help="Auto-discover host via mDNS")
    p.add_argument("--port", type=int, default=CONTROL_PORT)
    p.add_argument("--video-port", type=int, default=VIDEO_PORT)
    p.add_argument("--audio-port", type=int, default=AUDIO_PORT)
    p.add_argument("--fullscreen", action="store_true")
    p.add_argument("--grab-mouse", action="store_true", help="Lock mouse (FPS mode)")
    p.add_argument("--no-audio", action="store_true", help="Disable audio")
    p.add_argument("--no-encryption", action="store_true", help="Disable encryption")
    p.add_argument("--fingerprint", type=str, default=None,
                   help="Expected TLS certificate fingerprint (SHA-256)")
    p.add_argument("--bitrate", type=int, default=8_000_000,
                   help="Max bitrate for adaptive control (bps)")
    # Relay support (reserved for future use with relay transport)
    p.add_argument("--relay", type=str, default=None,
                   help="Relay server address host:port")
    p.add_argument("--room", type=str, default=None,
                   help="Relay room code (4-char hex)")
    # Pairing
    p.add_argument("--trust-new", action="store_true",
                   help="Accept changed host certificate and update pairing")
    p.add_argument("--forget", action="store_true",
                   help="Remove saved pairing for this host and exit")
    p.add_argument("--list-paired", action="store_true",
                   help="List all paired hosts and exit")
    args = p.parse_args()

    # Pairing management commands
    if args.list_paired:
        hosts = KnownHosts().all()
        if not hosts:
            print("  No paired devices.")
        else:
            print("\n  Paired devices:\n")
            for addr, info in hosts.items():
                name = info.get("name", "")
                fp = info.get("fingerprint", "?")
                seen = info.get("last_seen", "?")[:10]
                label = f"{name} ({addr})" if name else addr
                print(f"    {label}")
                print(f"      Fingerprint: {fp}")
                print(f"      Last seen:   {seen}\n")
        return

    if args.forget:
        host = getattr(args, "host", None)
        if not host or host == "auto":
            print("  Specify host IP: python client.py <ip> --forget")
            return
        kh = KnownHosts()
        kh.remove(host, args.port)
        print(f"  Pairing removed for {host}:{args.port}")
        return

    # --auto flag or host == "auto" both trigger mDNS discovery
    if args.auto:
        args.host = "auto"

    GameStreamClient(args).start()


if __name__ == "__main__":
    main()
