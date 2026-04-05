#!/usr/bin/env python3
"""
GameStream Host v2 — H.264 + Audio + TLS Encryption.

Captures screen (H.264 or MJPEG), system audio (Opus or PCM),
sends both over encrypted UDP channels. Input received over TLS.

Usage:
    python host.py [--port 9900] [--fps 60] [--bitrate 8000000]
                   [--no-audio] [--no-encryption] [--monitor 0]
                   [--relay host:port] [--room XXXX]
"""

import argparse
import socket
import struct
import threading
import time
import sys
import signal
import os

try:
    import numpy as np
    import mss
except ImportError as e:
    print(f"❌  Missing core dependency: {e}")
    print("    pip install numpy mss")
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
    pack_message, recv_message, chunk_frame, pack_audio, MAGIC, VERSION,
)
from shared.video_codec import create_encoder, detect_hw_encoder
from shared.crypto import (
    ensure_certificates, create_server_ssl_context, get_cert_fingerprint,
    SessionCipher, encode_session_key,
)
from shared.relay_transport import RelayChannel

# ── Input simulation ──────────────────────────────────────────────────
INPUT_BACKEND = None
try:
    from pynput.keyboard import Controller as KBCtrl, Key as PKey
    from pynput.mouse import Controller as MCtrl, Button as MBtn
    INPUT_BACKEND = "pynput"
except ImportError:
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
        INPUT_BACKEND = "pyautogui"
    except ImportError:
        pass

SPECIAL_KEYS = {}
if INPUT_BACKEND == "pynput":
    SPECIAL_KEYS = {
        "return": PKey.enter, "escape": PKey.esc, "tab": PKey.tab,
        "space": PKey.space, "backspace": PKey.backspace, "delete": PKey.delete,
        "up": PKey.up, "down": PKey.down, "left": PKey.left, "right": PKey.right,
        "home": PKey.home, "end": PKey.end, "page_up": PKey.page_up,
        "page_down": PKey.page_down, "insert": PKey.insert,
        "left shift": PKey.shift_l, "right shift": PKey.shift_r,
        "left ctrl": PKey.ctrl_l, "right ctrl": PKey.ctrl_r,
        "left alt": PKey.alt_l, "right alt": PKey.alt_r,
        "caps lock": PKey.caps_lock, "num lock": PKey.num_lock,
        "left meta": PKey.cmd_l, "right meta": PKey.cmd_r,
        **{f"f{i}": getattr(PKey, f"f{i}") for i in range(1, 13)},
    }


class InputSimulator:
    def __init__(self):
        self.backend = INPUT_BACKEND
        if self.backend == "pynput":
            self.kb = KBCtrl()
            self.mouse = MCtrl()
        self.scr_w, self.scr_h = 1920, 1080

    def set_screen(self, w, h):
        self.scr_w, self.scr_h = w, h

    def handle(self, ev: dict):
        if not self.backend:
            return
        try:
            it = ev.get("input_type")
            if it == InputType.KEY_DOWN:
                self._key(ev["key"], True)
            elif it == InputType.KEY_UP:
                self._key(ev["key"], False)
            elif it == InputType.MOUSE_MOVE:
                self._move(ev)
            elif it == InputType.MOUSE_BUTTON:
                self._btn(ev)
            elif it == InputType.MOUSE_SCROLL:
                self._scroll(ev)
            elif it in (InputType.GAMEPAD_AXIS, InputType.GAMEPAD_BUTTON, InputType.GAMEPAD_HAT):
                self._gamepad(ev)
        except Exception:
            pass

    def _key(self, name, press):
        if self.backend == "pynput":
            k = SPECIAL_KEYS.get(name.lower(), name if len(name) == 1 else None)
            if k is None:
                return
            (self.kb.press if press else self.kb.release)(k)
        else:
            import pyautogui
            (pyautogui.keyDown if press else pyautogui.keyUp)(name)

    def _move(self, ev):
        if ev.get("relative"):
            if self.backend == "pynput":
                self.mouse.move(int(ev["x"]), int(ev["y"]))
            else:
                import pyautogui; pyautogui.moveRel(int(ev["x"]), int(ev["y"]), _pause=False)
        else:
            ax, ay = int(ev["x"] * self.scr_w), int(ev["y"] * self.scr_h)
            if self.backend == "pynput":
                self.mouse.position = (ax, ay)
            else:
                import pyautogui; pyautogui.moveTo(ax, ay, _pause=False)

    def _btn(self, ev):
        bmap_pynput = {1: MBtn.left, 2: MBtn.middle, 3: MBtn.right}
        bmap_pyag = {1: "left", 2: "middle", 3: "right"}
        if self.backend == "pynput":
            b = bmap_pynput.get(ev["button"], MBtn.left)
            (self.mouse.press if ev["pressed"] else self.mouse.release)(b)
        else:
            import pyautogui
            b = bmap_pyag.get(ev["button"], "left")
            (pyautogui.mouseDown if ev["pressed"] else pyautogui.mouseUp)(button=b, _pause=False)

    def _scroll(self, ev):
        dy = ev.get("dy", 0)
        if self.backend == "pynput":
            self.mouse.scroll(0, dy)
        else:
            import pyautogui; pyautogui.scroll(dy, _pause=False)

    def _gamepad(self, ev):
        try:
            import vgamepad as vg
            if not hasattr(self, '_vpad'):
                self._vpad = vg.VX360Gamepad()
            it = ev["input_type"]
            if it == InputType.GAMEPAD_AXIS:
                axis, val = ev["axis"], ev["value"]
                iv = int(val * 32767)
                if axis == 0: self._vpad.left_joystick(x_value=iv, y_value=0)
                elif axis == 1: self._vpad.left_joystick(x_value=0, y_value=-iv)
                elif axis == 2: self._vpad.right_joystick(x_value=iv, y_value=0)
                elif axis == 3: self._vpad.right_joystick(x_value=0, y_value=-iv)
                elif axis == 4: self._vpad.left_trigger(value=int(max(0, val) * 255))
                elif axis == 5: self._vpad.right_trigger(value=int(max(0, val) * 255))
                self._vpad.update()
            elif it == InputType.GAMEPAD_BUTTON:
                bm = {0: vg.XUSB_BUTTON.XUSB_GAMEPAD_A, 1: vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                      2: vg.XUSB_BUTTON.XUSB_GAMEPAD_X, 3: vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
                      4: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
                      5: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
                      6: vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
                      7: vg.XUSB_BUTTON.XUSB_GAMEPAD_START}
                b = bm.get(ev["button"])
                if b:
                    (self._vpad.press_button if ev["pressed"] else self._vpad.release_button)(button=b)
                    self._vpad.update()
        except ImportError:
            pass


class ScreenCapture:
    """
    Screen capture with two backends:

    1. dxcam  (preferred on Windows) — Direct3D 11 capture. Does NOT use
       DXGI Desktop Duplication, so Chrome/Edge do NOT detect it as screen
       recording and therefore do NOT block DRM-protected video (YouTube, etc.).
       Runs in video mode: get_latest_frame() blocks until the next frame is
       ready, providing accurate frame-rate pacing without busy-waiting.
       pip install dxcam

    2. mss (fallback) — DXGI Desktop Duplication. Fast but Chrome detects
       it and replaces hardware-accelerated video surfaces with a black/error
       overlay when DRM content is playing.
    """

    def __init__(self, monitor_idx=0, scale=1.0):
        with mss.mss() as sct:
            monitors = sct.monitors
            self.monitor = dict(monitors[min(monitor_idx + 1, len(monitors) - 1)])
        self.scale = scale
        self.width  = int(self.monitor["width"]  * scale)
        self.height = int(self.monitor["height"] * scale)
        self.raw_w  = self.monitor["width"]
        self.raw_h  = self.monitor["height"]
        self._sct        = None
        self._dxcam      = None
        self._video_mode = False
        self._backend    = "mss"

        if sys.platform == "win32":
            try:
                import dxcam
                self._dxcam = dxcam.create(output_idx=monitor_idx,
                                            output_color="BGR")
                self._backend = "dxcam"
                print(f"  📸  Capture backend: dxcam (DRM-compatible)")
            except ImportError:
                print(f"  📸  Capture backend: mss  "
                      f"(install dxcam for DRM-compatible capture: pip install dxcam)")
            except Exception as e:
                print(f"  📸  Capture backend: mss  (dxcam init failed: {e})")

    def start_video_mode(self, fps: int) -> bool:
        if self._dxcam is not None and not self._video_mode:
            try:
                self._dxcam.start(target_fps=fps, video_mode=True)
                self._video_mode = True
                print(f"  📸  dxcam video mode: {fps} fps")
                return True
            except Exception as e:
                print(f"  📸  dxcam video mode failed ({e}), using grab()")
        return False

    def stop_video_mode(self):
        if self._dxcam is not None and self._video_mode:
            try:
                self._dxcam.stop()
            except Exception:
                pass
            self._video_mode = False

    def grab(self):
        if self._dxcam is not None:
            if self._video_mode:
                frame = self._dxcam.get_latest_frame()
            else:
                frame = self._dxcam.grab()
            if frame is not None:
                if self.scale != 1.0:
                    import cv2
                    frame = cv2.resize(frame, (self.width, self.height),
                                       interpolation=cv2.INTER_LINEAR)
                return frame[:, :, ::-1].copy()
            if self._video_mode:
                return None

        if self._sct is None:
            self._sct = mss.mss()
        img = self._sct.grab(self.monitor)
        frame = np.array(img, dtype=np.uint8)[:, :, :3]
        if self.scale != 1.0:
            import cv2
            frame = cv2.resize(frame, (self.width, self.height),
                               interpolation=cv2.INTER_LINEAR)
        return frame[:, :, ::-1].copy()


class GameStreamHost:
    def __init__(self, args):
        self.args = args
        self.running = False
        self.clients = {}
        self.input_sim = InputSimulator()
        self.capture = ScreenCapture(args.monitor, args.scale)
        self.input_sim.set_screen(self.capture.raw_w, self.capture.raw_h)

        # Encryption
        self.use_encryption = not args.no_encryption
        self.cipher = None
        self.session_key = None
        if self.use_encryption:
            self.session_key = SessionCipher.generate_key()
            self.cipher = SessionCipher(self.session_key)

        # Video encoder
        self.encoder = create_encoder(
            self.capture.width, self.capture.height,
            fps=args.fps, bitrate=args.bitrate,
            prefer_hw=not args.sw_encode,
            fallback_quality=args.quality,
        )

        # Force keyframe flag
        self._force_keyframe = threading.Event()

        # Audio
        self.use_audio = not args.no_audio
        self.audio_capture = None
        self.audio_encoder = None
        if self.use_audio:
            try:
                from shared.audio_stream import AudioCapture, create_audio_encoder, audio_available
                if audio_available():
                    self.audio_capture = AudioCapture(device=args.audio_device)
                    self.audio_encoder = create_audio_encoder()
                else:
                    print("  ⚠️  Audio dependencies missing, audio disabled")
                    self.use_audio = False
            except Exception as e:
                print(f"  ⚠️  Audio init failed: {e}")
                self.use_audio = False

        # mDNS announcer (started in start())
        self._announcer = None

        # Stats
        self.fps_actual = 0.0
        self.bytes_sent_video = 0
        self.bytes_sent_audio = 0

    def start(self):
        self.running = True
        signal.signal(signal.SIGINT, lambda *_: self.stop())

        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.winmm.timeBeginPeriod(1)
            except Exception:
                pass

        # TLS setup
        cert_path, key_path = None, None
        fingerprint = "N/A"
        if self.use_encryption:
            cert_path, key_path = ensure_certificates()
            fingerprint = get_cert_fingerprint(cert_path)
            self.ssl_ctx = create_server_ssl_context(cert_path, key_path)

        import socket as _sock
        _s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        try:
            _s.connect(("8.8.8.8", 80))
            local_ip = _s.getsockname()[0]
        except Exception:
            local_ip = "127.0.0.1"
        finally:
            _s.close()

        client_url = f"python client.py {local_ip} --port {self.args.port}"

        print()
        print(f"╔══════════════════════════════════════════════════════╗")
        print(f"║           🎮  GameStream Host  v2.0                 ║")
        print(f"╠══════════════════════════════════════════════════════╣")
        print(f"║  Host IP      : {local_ip:<36}║")
        print(f"║  Control port : {self.args.port:<36}║")
        print(f"║  Video port   : {self.args.port + 1:<36}║")
        print(f"║  Audio port   : {self.args.port + 2:<36}║")
        print(f"║  Resolution   : {self.capture.width}x{self.capture.height}"
              f"{'':>{30 - len(f'{self.capture.width}x{self.capture.height}')}}║")
        print(f"║  Video codec  : {self.encoder.codec_name:<36}║")
        print(f"║  Target FPS   : {self.args.fps:<36}║")
        print(f"║  Bitrate      : {self.args.bitrate // 1_000_000} Mbps"
              f"{'':>{32 - len(str(self.args.bitrate // 1_000_000))}}║")
        enc_str = "TLS + AES-256-GCM" if self.use_encryption else "DISABLED"
        print(f"║  Encryption   : {enc_str:<36}║")
        aud_str = "Enabled" if self.use_audio else "Disabled"
        print(f"║  Audio        : {aud_str:<36}║")
        print(f"║  Input backend: {(INPUT_BACKEND or 'none'):<36}║")
        print(f"╚══════════════════════════════════════════════════════╝")
        print(f"\n  💻  Connect with:")
        print(f"      {client_url}")
        if self.use_encryption:
            print(f"\n  🔐  TLS fingerprint (pass to client with --fingerprint):")
            print(f"      {fingerprint}")
        print(f"\n  Waiting for client connections...\n")

        # Relay mode
        self._relay_video: RelayChannel | None = None
        self._relay_audio: RelayChannel | None = None
        relay_active = bool(getattr(self.args, "relay", None))

        if relay_active:
            relay_addr = self.args.relay
            room = (self.args.room or "").strip().upper()
            if not room:
                import secrets as _sec
                room = _sec.token_hex(2).upper()
            self.args.room = room

            print()
            print(f"╔══════════════════════════════════════════════════════╗")
            print(f"║           Relay Mode — room {room:<24}║")
            print(f"╠══════════════════════════════════════════════════════╣")
            print(f"║  Relay   : {relay_addr:<41}║")
            print(f"║  Room    : {room:<41}║")
            print(f"╚══════════════════════════════════════════════════════╝")
            print(f"\n  Share with client:")
            print(f"      python client.py --relay {relay_addr} --room {room}\n")
        else:
            # mDNS announcement (LAN mode only)
            try:
                from shared.discovery import ServiceAnnouncer
                self._announcer = ServiceAnnouncer(
                    "GameStream",
                    self.args.port,
                    {"encrypted": str(int(self.use_encryption))},
                )
                self._announcer.start()
            except Exception as e:
                print(f"  ⚠️  mDNS announce failed: {e}")

        # Threads
        if relay_active:
            relay_addr = self.args.relay
            room = self.args.room

            # Control channel via relay
            relay_ctrl = RelayChannel(relay_addr, room, "host", "control")
            threads = [
                threading.Thread(
                    target=self._relay_control_connector,
                    args=(relay_ctrl,),
                    daemon=True, name="relay-control",
                ),
                threading.Thread(target=self._video_streamer, daemon=True, name="video"),
                threading.Thread(target=self._stats_printer, daemon=True, name="stats"),
            ]

            # Video relay channel
            self._relay_video = RelayChannel(relay_addr, room, "host", "video")
            threads.append(threading.Thread(
                target=self._relay_channel_connector,
                args=(self._relay_video, "video"),
                daemon=True, name="relay-video",
            ))

            # Audio relay channel
            if self.use_audio:
                self._relay_audio = RelayChannel(relay_addr, room, "host", "audio")
                threads.append(threading.Thread(
                    target=self._relay_channel_connector,
                    args=(self._relay_audio, "audio"),
                    daemon=True, name="relay-audio",
                ))
                threads.append(threading.Thread(
                    target=self._audio_streamer, daemon=True, name="audio"
                ))
        else:
            threads = [
                threading.Thread(target=self._control_server, daemon=True, name="control"),
                threading.Thread(target=self._video_streamer, daemon=True, name="video"),
                threading.Thread(target=self._stats_printer, daemon=True, name="stats"),
            ]
            if self.use_audio:
                threads.append(threading.Thread(target=self._audio_streamer, daemon=True, name="audio"))

        for t in threads:
            t.start()

        try:
            while self.running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        self.stop()

    def stop(self):
        if not self.running:
            return
        self.running = False
        self.capture.stop_video_mode()
        self.encoder.close()
        if self.audio_capture:
            self.audio_capture.stop()
        if self.audio_encoder:
            self.audio_encoder.close()
        if self._announcer:
            try:
                self._announcer.stop()
            except Exception:
                pass
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        print("\n🛑  Host shutting down...")

    # ── Relay connectors ─────────────────────────────────────────────

    def _relay_control_connector(self, channel: RelayChannel):
        """Connect the relay control channel and handle the paired client."""
        print(f"  📡  Relay control: connecting...")
        while self.running:
            try:
                channel.connect()
                print(f"  📡  Relay control: paired with client")
                channel.settimeout(5.0)
                addr = ("relay", 0)
                self._handle_relay_client(channel, addr)
            except (ConnectionResetError, OSError, TimeoutError) as e:
                print(f"  ⚠️  Relay control error: {e}")
            if self.running:
                print(f"  🔄  Relay control: reconnecting in 3s...")
                time.sleep(3)

    def _relay_channel_connector(self, channel: RelayChannel, name: str):
        """Connect a relay video/audio channel and keep it alive."""
        print(f"  📡  Relay {name}: connecting...")
        while self.running:
            try:
                channel.connect()
                print(f"  📡  Relay {name}: ready")
                # Block until the channel dies (send/recv error or close).
                # This replaces the old time.sleep(1.0) poll loop and means
                # we reconnect immediately when the peer disconnects.
                channel.wait_until_dead()
                if not self.running:
                    break
                print(f"  ⚠️  Relay {name}: connection lost")
            except (ConnectionResetError, OSError, TimeoutError) as e:
                print(f"  ⚠️  Relay {name} error: {e}")
            try:
                channel.close()
            except Exception:
                pass
            if self.running:
                print(f"  🔄  Relay {name}: reconnecting in 3s...")
                time.sleep(3)

    def _handle_relay_client(self, channel: RelayChannel, addr):
        """Same logic as _handle_client but operates on a RelayChannel."""
        channel.settimeout(5.0)
        self.clients[addr] = {"conn": channel, "video_addr": None, "audio_addr": None, "alive": True}
        try:
            while self.running and self.clients[addr]["alive"]:
                try:
                    msg = recv_message(channel)
                except socket.timeout:
                    continue
                if msg is None:
                    break
                # In relay mode: set a sentinel video/audio_addr so streamers know a client exists
                if msg.get("type") == MsgType.HANDSHAKE:
                    if self.clients[addr]["video_addr"] is None:
                        self.clients[addr]["video_addr"] = ("relay", 0)
                        self.clients[addr]["audio_addr"] = ("relay", 0)
                self._process_msg(msg, addr)
        except Exception as e:
            print(f"  ⚠️  Relay client error: {e}")
        finally:
            print(f"  📴  Relay client disconnected")
            self.clients.pop(addr, None)
            channel.close()

    # ── TCP/TLS Control Server ────────────────────────────────────────
    def _control_server(self):
        raw_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        raw_srv.settimeout(1.0)
        raw_srv.bind(("0.0.0.0", self.args.port))
        raw_srv.listen(4)

        while self.running:
            try:
                raw_conn, addr = raw_srv.accept()
            except socket.timeout:
                continue

            if self.use_encryption:
                try:
                    conn = self.ssl_ctx.wrap_socket(raw_conn, server_side=True)
                except Exception as e:
                    print(f"  ⚠️  TLS handshake failed from {addr[0]}: {e}")
                    raw_conn.close()
                    continue
            else:
                conn = raw_conn

            print(f"  📡  Client connected: {addr[0]}:{addr[1]}"
                  f"{' (TLS)' if self.use_encryption else ''}")
            t = threading.Thread(target=self._handle_client, args=(conn, addr), daemon=True)
            t.start()
        raw_srv.close()

    def _handle_client(self, conn, addr):
        conn.settimeout(5.0)
        self.clients[addr] = {"conn": conn, "video_addr": None, "audio_addr": None, "alive": True}
        try:
            while self.running and self.clients[addr]["alive"]:
                try:
                    msg = recv_message(conn)
                except socket.timeout:
                    continue
                if msg is None:
                    break
                self._process_msg(msg, addr)
        except Exception as e:
            print(f"  ⚠️  Client {addr[0]} error: {e}")
        finally:
            print(f"  📴  Client disconnected: {addr[0]}:{addr[1]}")
            self.clients.pop(addr, None)
            conn.close()

    def _process_msg(self, msg, addr):
        mt = msg.get("type")

        if mt == MsgType.HANDSHAKE:
            relay_active = bool(getattr(self.args, "relay", None))
            if relay_active:
                # In relay mode, video/audio go through relay channels
                self.clients[addr]["video_addr"] = ("relay", 0)
                self.clients[addr]["audio_addr"] = ("relay", 0)
            else:
                vp = msg.get("video_port", self.args.port + 1)
                ap = msg.get("audio_port", self.args.port + 2)
                self.clients[addr]["video_addr"] = (addr[0], vp)
                self.clients[addr]["audio_addr"] = (addr[0], ap)

            config_data = {
                "width": self.capture.width,
                "height": self.capture.height,
                "fps": self.args.fps,
                "bitrate": self.args.bitrate,
                "codec": self.encoder.codec_name,
                "audio_enabled": self.use_audio,
                "encrypted": self.use_encryption,
                "hostname": socket.gethostname(),
            }
            if self.use_encryption and self.session_key:
                config_data["session_key"] = encode_session_key(self.session_key)

            self.clients[addr]["conn"].sendall(pack_message(MsgType.CONFIG, config_data))
            if relay_active:
                print(f"  ✅  Handshake OK: {addr[0]} (relay)")
            else:
                print(f"  ✅  Handshake OK: {addr[0]} → video:{vp} audio:{ap}")

        elif mt == MsgType.INPUT_EVENT:
            self.input_sim.handle(msg)

        elif mt == MsgType.PING:
            try:
                self.clients[addr]["conn"].sendall(
                    pack_message(MsgType.PONG, {"ts": msg.get("ts", 0)})
                )
            except Exception:
                pass

        elif mt == MsgType.QUALITY_ADJUST:
            new_br = msg.get("bitrate")
            if new_br:
                self.encoder.set_bitrate(new_br)
                print(f"  📊  Bitrate adjusted to {new_br // 1_000_000} Mbps")

        elif mt == MsgType.CLIPBOARD:
            if HAS_CLIPBOARD:
                direction = msg.get("direction", "push")
                if direction == "push":
                    text = msg.get("text", "")
                    try:
                        pyperclip.copy(text)
                        print(f"  📋  Clipboard received from client ({len(text)} chars)")
                    except Exception as e:
                        print(f"  ⚠️  Clipboard write failed: {e}")
                elif direction == "pull":
                    try:
                        text = pyperclip.paste()
                        self.clients[addr]["conn"].sendall(
                            pack_message(MsgType.CLIPBOARD, {"text": text})
                        )
                    except Exception as e:
                        print(f"  ⚠️  Clipboard pull failed: {e}")
            else:
                if msg.get("direction") == "pull":
                    try:
                        self.clients[addr]["conn"].sendall(
                            pack_message(MsgType.CLIPBOARD, {"text": ""})
                        )
                    except Exception:
                        pass

        elif mt == MsgType.FORCE_KEYFRAME:
            self._force_keyframe.set()
            print(f"  🔑  Force keyframe requested by {addr[0]}")

        elif mt == MsgType.DISCONNECT:
            self.clients[addr]["alive"] = False

    # ── Video Streamer ────────────────────────────────────────────────
    def _video_streamer(self):
        relay_active = bool(getattr(self.args, "relay", None))
        sock = None
        if not relay_active:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)

        frame_interval = 1.0 / self.args.fps
        frame_id = 0
        fps_counter = 0
        fps_timer = time.time()
        keyframe_every = self.args.fps * 1  # IDR every 1 second

        use_video_mode = self.capture.start_video_mode(self.args.fps)

        # In relay mode: wait for relay_video to be connected before streaming
        if relay_active:
            print("  📡  Video streamer waiting for relay channel...")
            while self.running and self._relay_video is None:
                time.sleep(0.2)

        while self.running:
            t0 = time.perf_counter()

            if relay_active:
                # Stream if relay video channel is ready (connected) or any regular client
                relay_ready = (
                    self._relay_video is not None
                    and self._relay_video._sock is not None
                )
                video_addrs = [c["video_addr"] for c in self.clients.values()
                               if c.get("video_addr") and c["video_addr"] != ("relay", 0)]
                has_targets = relay_ready or bool(video_addrs)
            else:
                video_addrs = [c["video_addr"] for c in self.clients.values() if c.get("video_addr")]
                has_targets = bool(video_addrs)

            if not has_targets:
                time.sleep(0.1)
                continue

            frame_rgb = self.capture.grab()
            if frame_rgb is None:
                continue

            # Check for forced keyframe (from client FORCE_KEYFRAME request)
            force_kf = (frame_id % keyframe_every == 0)
            if self._force_keyframe.is_set():
                force_kf = True
                self._force_keyframe.clear()

            encoded = self.encoder.encode(frame_rgb, force_keyframe=force_kf)
            if not encoded:
                continue

            if self.cipher:
                encrypted = self.cipher.encrypt(encoded)
            else:
                encrypted = encoded

            packets = chunk_frame(frame_id, encrypted)

            if relay_active and relay_ready:
                for pkt in packets:
                    try:
                        self._relay_video.sendto(pkt, None)
                    except OSError:
                        pass

            if not relay_active:
                for addr in video_addrs:
                    for pkt in packets:
                        try:
                            sock.sendto(pkt, addr)
                        except OSError:
                            pass

            self.bytes_sent_video += len(encrypted)
            frame_id += 1
            fps_counter += 1

            now = time.time()
            if now - fps_timer >= 1.0:
                self.fps_actual = fps_counter / (now - fps_timer)
                fps_counter = 0
                fps_timer = now

            if not use_video_mode:
                elapsed = time.perf_counter() - t0
                remaining = frame_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)

        if sock:
            sock.close()

    # ── Audio Streamer ────────────────────────────────────────────────
    def _audio_streamer(self):
        if not self.audio_capture or not self.audio_encoder:
            return

        relay_active = bool(getattr(self.args, "relay", None))
        sock = None
        if not relay_active:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        self.audio_capture.start()
        seq = 0
        start_time = time.time()

        while self.running:
            if relay_active:
                relay_ready = (
                    self._relay_audio is not None
                    and self._relay_audio._sock is not None
                )
                audio_addrs = [c["audio_addr"] for c in self.clients.values()
                               if c.get("audio_addr") and c["audio_addr"] != ("relay", 0)]
                has_targets = relay_ready or bool(audio_addrs)
            else:
                audio_addrs = [c["audio_addr"] for c in self.clients.values() if c.get("audio_addr")]
                has_targets = bool(audio_addrs)

            if not has_targets:
                time.sleep(0.05)
                continue

            pcm = self.audio_capture.read()
            if pcm is None:
                continue

            encoded = self.audio_encoder.encode(pcm)
            if not encoded:
                continue

            ts_ms = int((time.time() - start_time) * 1000)
            packet = pack_audio(seq, ts_ms, encoded)

            if self.cipher:
                packet = self.cipher.encrypt(packet)

            if relay_active and relay_ready:
                try:
                    self._relay_audio.sendto(packet, None)
                except OSError:
                    pass
            elif not relay_active:
                for addr in audio_addrs:
                    try:
                        sock.sendto(packet, addr)
                    except OSError:
                        pass

            self.bytes_sent_audio += len(packet)
            seq += 1

        if sock:
            sock.close()

    # ── Stats ─────────────────────────────────────────────────────────
    def _stats_printer(self):
        while self.running:
            time.sleep(3)
            c = len(self.clients)
            vm = self.bytes_sent_video / (1024 * 1024)
            am = self.bytes_sent_audio / (1024 * 1024)
            enc = "🔒" if self.use_encryption else "🔓"
            sys.stdout.write(
                f"\r  {enc} FPS:{self.fps_actual:.0f} | "
                f"Clients:{c} | Video:{vm:.1f}MB | Audio:{am:.1f}MB   "
            )
            sys.stdout.flush()


def main():
    p = argparse.ArgumentParser(description="GameStream Host v2")
    p.add_argument("--port", type=int, default=CONTROL_PORT)
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--bitrate", type=int, default=8_000_000, help="H.264 bitrate (bps)")
    p.add_argument("--quality", type=int, default=65, help="MJPEG fallback quality")
    p.add_argument("--monitor", type=int, default=0)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--sw-encode", action="store_true", help="Force software encoding")
    p.add_argument("--no-audio", action="store_true", help="Disable audio streaming")
    p.add_argument("--no-encryption", action="store_true", help="Disable TLS/AES encryption")
    p.add_argument("--audio-device", type=int, default=None, help="Audio device index")
    p.add_argument("--list-audio", action="store_true", help="List audio devices and exit")
    # Relay support (reserved for future use with relay transport)
    p.add_argument("--relay", type=str, default=None,
                   help="Relay server address host:port")
    p.add_argument("--room", type=str, default=None,
                   help="Relay room code (4-char hex)")
    args = p.parse_args()

    if args.list_audio:
        from shared.audio_stream import AudioCapture
        AudioCapture.list_devices()
        return

    GameStreamHost(args).start()


if __name__ == "__main__":
    main()
