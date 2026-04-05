#!/usr/bin/env python3
"""
GameStream Mobile Gateway v2 — H.264 + Audio + Auth + Flow Control.

Improvements over v1:
  - H.264 video (3–5x less bandwidth than JPEG)
  - Raw PCM audio streaming via WebAudio
  - Token authentication (printed on startup, required in WS URL)
  - Per-client flow control: asyncio.Queue(maxsize=1) drops old frames
    instead of buffering — client always receives the latest frame
  - Dirty frame detection: skip encoding when screen hasn't changed
  - Conditional encoding: skip H264/JPEG encode when no clients need it
  - Wake-on-LAN via POST /wol or WS message
  - Clipboard sync via WS messages
  - Force-keyframe support
  - vgamepad XInput virtual controller (WASD keyboard fallback)
  - Fixed _key_states class-variable bug → instance variable

Usage:
    python gateway.py [--port 8080] [--fps 30] [--scale 0.75] [--monitor 0]
                      [--no-h264] [--tls]
    Open http://<host_ip>:8080/?token=<TOKEN> on your phone.
"""

import argparse
import asyncio
import json
import os
import secrets
import struct
import sys
import threading
import time

# Force UTF-8 stdout/stderr on Windows to avoid UnicodeEncodeError on emoji
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import numpy as np
    import cv2
    import mss
    from aiohttp import web
    import aiohttp
except ImportError as e:
    print(f"❌  Missing dependency: {e}")
    print("    pip install numpy opencv-python mss aiohttp")
    sys.exit(1)

# Optional: pyperclip for clipboard sync
try:
    import pyperclip
    HAS_CLIPBOARD = True
except ImportError:
    HAS_CLIPBOARD = False

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
        "enter": PKey.enter, "escape": PKey.esc, "tab": PKey.tab,
        "space": PKey.space, "backspace": PKey.backspace, "delete": PKey.delete,
        "up": PKey.up, "down": PKey.down, "left": PKey.left, "right": PKey.right,
        "shift": PKey.shift_l, "ctrl": PKey.ctrl_l, "alt": PKey.alt_l,
        **{f"f{i}": getattr(PKey, f"f{i}") for i in range(1, 13)},
    }

# Binary frame type prefix (first byte of every WS binary message)
FRAME_TYPE_VIDEO_JPEG = 0x00
FRAME_TYPE_VIDEO_H264 = 0x01
FRAME_TYPE_AUDIO      = 0x02


# ══════════════════════════════════════════════════════════════════════
#  Input Handler
# ══════════════════════════════════════════════════════════════════════

class InputHandler:
    """
    Routes mobile input events to pynput/pyautogui.
    Gamepad uses vgamepad (XInput virtual controller) when available,
    falls back to WASD keyboard mapping.
    """

    def __init__(self, screen_w: int, screen_h: int):
        self.scr_w = screen_w
        self.scr_h = screen_h
        self._key_states: dict = {}
        self._vpad = None
        self._vpad_tried = False
        if INPUT_BACKEND == "pynput":
            self.kb = KBCtrl()
            self.mouse = MCtrl()

    def handle(self, event: dict):
        if not INPUT_BACKEND:
            return
        try:
            t = event.get("type")
            if   t == "mouse_move":     self._mouse_move(event)
            elif t == "mouse_move_rel": self._mouse_move_rel(event)
            elif t == "mouse_down":     self._mouse_btn(event, True)
            elif t == "mouse_up":       self._mouse_btn(event, False)
            elif t == "mouse_scroll":   self._scroll(event)
            elif t == "key_down":       self._key(event["key"], True)
            elif t == "key_up":         self._key(event["key"], False)
            elif t == "gamepad_axis":   self._gamepad_axis(event)
            elif t == "gamepad_button": self._gamepad_button(event)
        except Exception:
            pass

    # ── Mouse ──────────────────────────────────────────────────────────

    def _mouse_move(self, ev):
        x, y = int(ev["x"] * self.scr_w), int(ev["y"] * self.scr_h)
        if INPUT_BACKEND == "pynput":
            self.mouse.position = (x, y)
        else:
            import pyautogui; pyautogui.moveTo(x, y, _pause=False)

    def _mouse_move_rel(self, ev):
        dx, dy = int(ev["dx"]), int(ev["dy"])
        if INPUT_BACKEND == "pynput":
            self.mouse.move(dx, dy)
        else:
            import pyautogui; pyautogui.moveRel(dx, dy, _pause=False)

    def _mouse_btn(self, ev, pressed: bool):
        btn = ev.get("button", 1)
        if INPUT_BACKEND == "pynput":
            b = {1: MBtn.left, 2: MBtn.middle, 3: MBtn.right}.get(btn, MBtn.left)
            (self.mouse.press if pressed else self.mouse.release)(b)
        else:
            import pyautogui
            b = {1: "left", 2: "middle", 3: "right"}.get(btn, "left")
            (pyautogui.mouseDown if pressed else pyautogui.mouseUp)(button=b, _pause=False)

    def _scroll(self, ev):
        dy = ev.get("dy", 0)
        if INPUT_BACKEND == "pynput":
            self.mouse.scroll(0, dy)
        else:
            import pyautogui; pyautogui.scroll(dy, _pause=False)

    # ── Keyboard ───────────────────────────────────────────────────────

    def _key(self, key_name: str, pressed: bool):
        if INPUT_BACKEND == "pynput":
            k = SPECIAL_KEYS.get(key_name.lower())
            if k:
                # Named special key (enter, shift, etc.)
                (self.kb.press if pressed else self.kb.release)(k)
            elif len(key_name) == 1:
                # Single character — check if it's ASCII printable or unicode accent
                if ord(key_name) > 127:
                    # Unicode / accent character: use kb.type() on press only
                    # (no release needed for typed chars)
                    if pressed:
                        self.kb.type(key_name)
                else:
                    (self.kb.press if pressed else self.kb.release)(key_name)
        else:
            import pyautogui
            (pyautogui.keyDown if pressed else pyautogui.keyUp)(key_name)

    def _set_key_state(self, key, active: bool):
        current = self._key_states.get(key, False)
        if active and not current:
            if INPUT_BACKEND == "pynput":
                self.kb.press(key)
            self._key_states[key] = True
        elif not active and current:
            if INPUT_BACKEND == "pynput":
                self.kb.release(key)
            self._key_states[key] = False

    # ── Gamepad ────────────────────────────────────────────────────────

    def _get_vpad(self):
        if self._vpad_tried:
            return self._vpad
        self._vpad_tried = True
        try:
            import vgamepad as vg
            self._vpad = vg.VX360Gamepad()
            print("  🎮  vgamepad XInput virtual controller active")
        except ImportError:
            print("  ⚠️  vgamepad not found — gamepad → WASD fallback")
            print("       pip install vgamepad  (requires ViGEmBus driver on Windows)")
        return self._vpad

    def _gamepad_axis(self, ev):
        axis = ev.get("axis", "left")
        x = float(ev.get("x", 0))
        y = float(ev.get("y", 0))

        vpad = self._get_vpad()
        if vpad:
            import vgamepad as vg
            ix = int(max(-1.0, min(1.0, x)) * 32767)
            iy = int(max(-1.0, min(1.0, -y)) * 32767)
            if axis == "left":
                vpad.left_joystick(x_value=ix, y_value=iy)
            else:
                vpad.right_joystick(x_value=ix, y_value=iy)
            vpad.update()
            return

        t = 0.3
        if INPUT_BACKEND == "pynput" and axis == "left":
            self._set_key_state("w", y < -t)
            self._set_key_state("s", y > t)
            self._set_key_state("a", x < -t)
            self._set_key_state("d", x > t)

    def _gamepad_button(self, ev):
        btn = ev.get("button", "").lower()
        pressed = ev.get("pressed", False)

        vpad = self._get_vpad()
        if vpad:
            import vgamepad as vg
            btn_map = {
                "a":          vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
                "b":          vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
                "x":          vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
                "y":          vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
                "lb":         vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
                "rb":         vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
                "start":      vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
                "select":     vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
                "dpad_up":    vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
                "dpad_down":  vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
                "dpad_left":  vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
                "dpad_right": vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
            }
            b = btn_map.get(btn)
            if b:
                (vpad.press_button if pressed else vpad.release_button)(button=b)
            if btn == "lt":
                vpad.left_trigger(value=255 if pressed else 0)
            elif btn == "rt":
                vpad.right_trigger(value=255 if pressed else 0)
            vpad.update()
            return

        km = {
            "a": "j", "b": "k", "x": "u", "y": "i",
            "lb": "q", "rb": "e", "lt": "z", "rt": "c",
            "start": "escape", "select": "tab",
            "dpad_up": "up", "dpad_down": "down",
            "dpad_left": "left", "dpad_right": "right",
        }
        k = km.get(btn)
        if k:
            self._key(k, pressed)


# ══════════════════════════════════════════════════════════════════════
#  Screen Streamer
# ══════════════════════════════════════════════════════════════════════

class ScreenStreamer:
    """
    Captures screen and encodes to H.264 (preferred) or JPEG (fallback).

    Conditional encoding: tracks how many clients need H264 vs JPEG.
    Skips encoding passes when no clients need that format.
    """

    def __init__(self, monitor_idx=0, scale=1.0, quality=60, max_fps=30,
                 use_h264=True):
        with mss.mss() as sct:
            monitors = sct.monitors
            self.monitor = dict(monitors[min(monitor_idx + 1, len(monitors) - 1)])
        self.scale = scale
        self.quality = quality
        self.max_fps = max_fps
        self.raw_w = self.monitor["width"]
        self.raw_h = self.monitor["height"]
        self.width = int(self.raw_w * scale)
        self.height = int(self.raw_h * scale)

        self._current_h264: bytes | None = None
        self._current_jpeg: bytes | None = None
        self._lock = threading.Lock()
        self._last_sample: bytes | None = None
        self._frame_id = 0
        self._kf_every = max_fps * 2
        self.running = False
        self.fps_actual = 0.0
        self.codec_name = "jpeg"
        self._dxcam = None
        self._dxcam_video_mode = False

        # Conditional encoding counters
        self._h264_clients = 0
        self._jpeg_clients = 0
        self._client_lock = threading.Lock()

        # Force keyframe flag
        self._force_keyframe = threading.Event()

        import sys as _sys
        if _sys.platform == "win32":
            try:
                import dxcam
                self._dxcam = dxcam.create(output_idx=monitor_idx, output_color="BGR")
                print(f"  📸  Capture backend: dxcam (DRM-compatible)")
            except ImportError:
                print(f"  📸  Capture backend: mss  (pip install dxcam for DRM support)")
            except Exception as e:
                print(f"  📸  Capture backend: mss  (dxcam unavailable: {e})")

        self.h264_encoder = None
        if use_h264:
            try:
                from shared.video_codec import H264Encoder, detect_hw_encoder
                codec = detect_hw_encoder()
                self.h264_encoder = H264Encoder(
                    self.width, self.height,
                    fps=max_fps,
                    bitrate=3_000_000,
                    codec_name=codec,
                    keyframe_interval=self._kf_every,
                )
                self.codec_name = "h264"
                print(f"  🎬  Mobile encoder: H.264 ({codec}) @ "
                      f"{self.width}x{self.height} 3 Mbps")
            except Exception as e:
                print(f"  ⚠️  H.264 unavailable ({e}), falling back to JPEG")

    def add_client(self, want_jpeg: bool):
        """Track a new client subscription. Call when client connects."""
        with self._client_lock:
            if want_jpeg or self.h264_encoder is None:
                self._jpeg_clients += 1
            else:
                self._h264_clients += 1

    def remove_client(self, want_jpeg: bool):
        """Remove a client subscription. Call when client disconnects."""
        with self._client_lock:
            if want_jpeg or self.h264_encoder is None:
                self._jpeg_clients = max(0, self._jpeg_clients - 1)
            else:
                self._h264_clients = max(0, self._h264_clients - 1)

    def update_client_codec(self, old_want_jpeg: bool, new_want_jpeg: bool):
        """Called when a client changes codec preference mid-stream."""
        with self._client_lock:
            if old_want_jpeg or self.h264_encoder is None:
                self._jpeg_clients = max(0, self._jpeg_clients - 1)
            else:
                self._h264_clients = max(0, self._h264_clients - 1)
            if new_want_jpeg or self.h264_encoder is None:
                self._jpeg_clients += 1
            else:
                self._h264_clients += 1

    def request_force_keyframe(self):
        """Request the next frame to be encoded as an IDR keyframe."""
        self._force_keyframe.set()

    def start(self):
        self.running = True
        if self._dxcam is not None:
            try:
                self._dxcam.start(target_fps=self.max_fps, video_mode=True)
                self._dxcam_video_mode = True
                print(f"  📸  dxcam video mode: {self.max_fps} fps")
            except Exception as e:
                print(f"  📸  dxcam grab mode (video mode unavailable: {e})")
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self._dxcam is not None and self._dxcam_video_mode:
            try:
                self._dxcam.stop()
            except Exception:
                pass
        if self.h264_encoder:
            self.h264_encoder.close()

    def _capture_loop(self):
        sct = mss.mss()
        fc, ft = 0, time.time()

        while self.running:
            t0 = time.perf_counter()
            interval = 1.0 / max(self.max_fps, 1)

            if self._dxcam is not None:
                if self._dxcam_video_mode:
                    frame = self._dxcam.get_latest_frame()
                    if frame is None:
                        continue
                else:
                    frame = self._dxcam.grab()
                    if frame is None:
                        time.sleep(0.002)
                        continue
            else:
                img = sct.grab(self.monitor)
                frame = np.array(img, dtype=np.uint8)[:, :, :3]

            if self.scale != 1.0:
                frame = cv2.resize(frame, (self.width, self.height),
                                   interpolation=cv2.INTER_LINEAR)

            sample = frame[::32, ::32].tobytes()
            force_kf = bool(self.h264_encoder and
                            self._frame_id % self._kf_every == 0)

            # Also check external force_keyframe request
            if self._force_keyframe.is_set():
                force_kf = True
                self._force_keyframe.clear()

            # Determine which encodings are needed
            with self._client_lock:
                need_h264 = self._h264_clients > 0
                need_jpeg = self._jpeg_clients > 0

            # Skip if screen hasn't changed — but always encode at least
            # one frame when a new client connects (current_* is still None)
            has_cached = (
                (not need_h264 or self._current_h264 is not None) and
                (not need_jpeg or self._current_jpeg is not None)
            )
            if sample == self._last_sample and not force_kf and has_cached:
                if not self._dxcam_video_mode:
                    elapsed = time.perf_counter() - t0
                    if elapsed < interval:
                        time.sleep(interval - elapsed)
                continue
            self._last_sample = sample

            # H264 encode (only when clients need it)
            h264_payload = None
            if self.h264_encoder and need_h264:
                try:
                    frame_rgb = frame[:, :, ::-1].copy()
                    encoded = self.h264_encoder.encode(frame_rgb,
                                                       force_keyframe=force_kf)
                    if encoded:
                        h264_payload = bytes([FRAME_TYPE_VIDEO_H264]) + encoded
                except Exception:
                    pass

            # JPEG encode (only when clients need it)
            jpeg_payload = None
            if need_jpeg:
                try:
                    _, buf = cv2.imencode(".jpg", frame,
                                          [cv2.IMWRITE_JPEG_QUALITY, self.quality])
                    jpeg_payload = bytes([FRAME_TYPE_VIDEO_JPEG]) + buf.tobytes()
                except Exception:
                    pass

            with self._lock:
                if h264_payload:
                    self._current_h264 = h264_payload
                if jpeg_payload:
                    self._current_jpeg = jpeg_payload

            self._frame_id += 1
            fc += 1
            now = time.time()
            if now - ft >= 1.0:
                self.fps_actual = fc / (now - ft)
                fc = 0
                ft = now

            if not self._dxcam_video_mode:
                elapsed = time.perf_counter() - t0
                if elapsed < interval:
                    time.sleep(interval - elapsed)

    def get_frame(self, want_jpeg: bool = False) -> bytes | None:
        with self._lock:
            if want_jpeg or self.h264_encoder is None:
                return self._current_jpeg
            return self._current_h264 or self._current_jpeg

    def set_quality(self, q: int):
        self.quality = max(10, min(95, q))


# ══════════════════════════════════════════════════════════════════════
#  Audio Streamer
# ══════════════════════════════════════════════════════════════════════

class AudioStreamer:
    """
    Captures system audio and broadcasts raw PCM to per-client asyncio queues.
    """

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []
        self._sub_lock = threading.Lock()
        self.running = False
        self.available = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._setup()

    def _setup(self):
        try:
            from shared.audio_stream import AudioCapture, audio_available
            if not audio_available():
                print("  ⚠️  Audio deps missing (sounddevice/numpy)")
                return
            self.capture = AudioCapture()
            self.available = True
            print("  🔈  Mobile audio: PCM/WebAudio streaming enabled")
        except Exception as e:
            print(f"  ⚠️  Audio unavailable: {e}")

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def start(self, loop: asyncio.AbstractEventLoop):
        if not self.available:
            return
        self._loop = loop
        self.running = True
        self.capture.start()
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.available and hasattr(self, "capture"):
            try:
                self.capture.stop()
            except Exception:
                pass

    @staticmethod
    def _enqueue(q: asyncio.Queue, item: bytes):
        if not q.full():
            q.put_nowait(item)

    def _capture_loop(self):
        seq = 0
        while self.running:
            pcm = self.capture.read()
            if pcm is None or self._loop is None:
                continue
            header = struct.pack("!I", seq & 0xFFFFFFFF)
            payload = bytes([FRAME_TYPE_AUDIO]) + header + pcm.astype("<f4").tobytes()
            seq += 1

            with self._sub_lock:
                subs = list(self._subscribers)
            for q in subs:
                self._loop.call_soon_threadsafe(AudioStreamer._enqueue, q, payload)


# ══════════════════════════════════════════════════════════════════════
#  Mobile Gateway
# ══════════════════════════════════════════════════════════════════════

class MobileGateway:
    """aiohttp WebSocket server — H.264 video + audio + auth + flow control."""

    def __init__(self, args):
        self.args = args
        self.token = secrets.token_hex(4)
        self.streamer = ScreenStreamer(
            monitor_idx=args.monitor,
            scale=args.scale,
            quality=args.quality,
            max_fps=args.fps,
            use_h264=not args.no_h264,
        )
        self.audio = AudioStreamer()
        self.input_handler = InputHandler(self.streamer.raw_w, self.streamer.raw_h)
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")

    async def start(self):
        loop = asyncio.get_running_loop()
        self.streamer.start()
        self.audio.start(loop)

        app = web.Application()
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/",   self._index_handler)
        app.router.add_post("/wol", self._wol_handler)
        app.router.add_static("/static/", self.static_dir, name="static")

        runner = web.AppRunner(app)
        await runner.setup()

        ssl_ctx = None
        port = self.args.port
        scheme = "http"
        if self.args.tls:
            from shared.crypto import ensure_certificates, create_server_ssl_context
            cert, key = ensure_certificates()
            ssl_ctx = create_server_ssl_context(cert, key)
            port = self.args.tls_port
            scheme = "https"

        await web.TCPSite(runner, "0.0.0.0", port, ssl_context=ssl_ctx).start()

        import socket as _sock
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        except Exception:
            local_ip = "localhost"
        finally:
            s.close()

        full_url = f"{scheme}://{local_ip}:{port}/?token={self.token}"

        # ── Relay mode ───────────────────────────────────────────────
        relay_addr = getattr(self.args, "relay", None)
        relay_room = (getattr(self.args, "room", None) or "").strip().upper()
        relay_task = None

        if relay_addr:
            if not relay_room:
                relay_room = secrets.token_hex(2).upper()
                self.args.room = relay_room

            # Determine relay host for URL display
            relay_host_part = relay_addr.rsplit(":", 1)[0] if ":" in relay_addr else relay_addr
            relay_port_part = relay_addr.rsplit(":", 1)[1] if ":" in relay_addr else "9951"
            relay_url = f"http://{relay_host_part}:{relay_port_part}/{relay_room}/?token={self.token}"

            W = 37
            print()
            print(f"╔══════════════════════════════════════════════════════╗")
            print(f"║      📱  GameStream Mobile Gateway  v2.0            ║")
            print(f"╠══════════════════════════════════════════════════════╣")
            print(f"║  Local URL : {full_url:<{W}}║")
            print(f"║  Relay URL : {relay_url:<{W}}║")
            print(f"║  Room      : {relay_room:<{W}}║")
            print(f"║  Resolution: {self.streamer.width}x{self.streamer.height}"
                  f"{'':>{W - len(f'{self.streamer.width}x{self.streamer.height}')}}║")
            print(f"║  Video     : {self.streamer.codec_name.upper():<{W}}║")
            print(f"║  Audio     : {'PCM/WebAudio' if self.audio.available else 'Disabled':<{W}}║")
            print(f"║  FPS       : {self.args.fps:<{W}}║")
            print(f"║  Input     : {INPUT_BACKEND or 'none':<{W}}║")
            print(f"╚══════════════════════════════════════════════════════╝")
            print(f"\n  📲  Share this URL with your phone:")
            print(f"      {relay_url}\n")

            relay_task = asyncio.create_task(
                self._relay_uplink_loop(relay_addr, relay_room)
            )
        else:
            W = 37
            print()
            print(f"╔══════════════════════════════════════════════════════╗")
            print(f"║      📱  GameStream Mobile Gateway  v2.0            ║")
            print(f"╠══════════════════════════════════════════════════════╣")
            print(f"║  Open      : {full_url:<{W}}║")
            print(f"║  Resolution: {self.streamer.width}x{self.streamer.height}"
                  f"{'':>{W - len(f'{self.streamer.width}x{self.streamer.height}')}}║")
            print(f"║  Video     : {self.streamer.codec_name.upper():<{W}}║")
            print(f"║  Audio     : {'PCM/WebAudio' if self.audio.available else 'Disabled':<{W}}║")
            print(f"║  FPS       : {self.args.fps:<{W}}║")
            print(f"║  TLS       : {'Enabled' if self.args.tls else 'Disabled':<{W}}║")
            print(f"║  Input     : {INPUT_BACKEND or 'none':<{W}}║")
            print(f"╚══════════════════════════════════════════════════════╝")
            print(f"\n  📲  {full_url}\n")

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            if relay_task:
                relay_task.cancel()
                try:
                    await relay_task
                except asyncio.CancelledError:
                    pass
            self.streamer.stop()
            self.audio.stop()
            await runner.cleanup()

    async def _relay_uplink_loop(self, relay_addr: str, room: str):
        """
        Maintain a persistent WebSocket connection to the relay /uplink/{room}
        endpoint. Incoming messages (from phone via relay) are handled like
        _ws_handler messages; outgoing video+audio are sent via the relay WS.
        Reconnects automatically on disconnect.
        """
        relay_host = relay_addr.rsplit(":", 1)[0] if ":" in relay_addr else relay_addr
        relay_port = relay_addr.rsplit(":", 1)[1] if ":" in relay_addr else "9951"
        uplink_url = f"ws://{relay_host}:{relay_port}/uplink/{room}?token={self.token}"

        while True:
            try:
                print(f"  📡  Relay uplink: connecting to {uplink_url}")
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(uplink_url, heartbeat=20) as relay_ws:
                        print(f"  📡  Relay uplink: connected (room={room})")

                        want_jpeg = [self.streamer.h264_encoder is None]
                        self.streamer.add_client(want_jpeg[0])

                        # Send initial config
                        await relay_ws.send_json({
                            "type":   "config",
                            "width":  self.streamer.width,
                            "height": self.streamer.height,
                            "fps":    self.args.fps,
                            "codec":  self.streamer.codec_name,
                            "audio":  self.audio.available,
                        })

                        stream_task = asyncio.create_task(
                            self._stream_to_client(relay_ws, want_jpeg)
                        )
                        try:
                            async for msg in relay_ws:
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        event = json.loads(msg.data)
                                        t = event.get("type")
                                        if t == "ping":
                                            await relay_ws.send_json(
                                                {"type": "pong", "ts": event.get("ts")}
                                            )
                                        elif t == "codec_pref":
                                            old = want_jpeg[0]
                                            new = (event.get("codec") == "jpeg")
                                            if old != new:
                                                self.streamer.update_client_codec(old, new)
                                                want_jpeg[0] = new
                                            codec = "jpeg" if want_jpeg[0] else self.streamer.codec_name
                                            await relay_ws.send_json({
                                                "type": "config",
                                                "width": self.streamer.width,
                                                "height": self.streamer.height,
                                                "fps": self.args.fps,
                                                "codec": codec,
                                                "audio": self.audio.available,
                                            })
                                        elif t == "wol":
                                            mac = event.get("mac", "")
                                            if mac:
                                                try:
                                                    from shared.wol import send_magic_packet
                                                    send_magic_packet(mac)
                                                    await relay_ws.send_json(
                                                        {"type": "wol_result", "status": "sent"}
                                                    )
                                                except Exception as e:
                                                    await relay_ws.send_json(
                                                        {"type": "wol_result", "error": str(e)}
                                                    )
                                        elif t == "clipboard_push":
                                            text = event.get("text", "")
                                            if HAS_CLIPBOARD:
                                                try:
                                                    import pyperclip
                                                    pyperclip.copy(text)
                                                    print(f"  📋  Clipboard from relay phone ({len(text)} chars)")
                                                except Exception as e:
                                                    print(f"  ⚠️  Clipboard write failed: {e}")
                                        elif t == "clipboard_pull":
                                            text = ""
                                            if HAS_CLIPBOARD:
                                                try:
                                                    import pyperclip
                                                    text = pyperclip.paste()
                                                except Exception:
                                                    pass
                                            await relay_ws.send_json({"type": "clipboard", "text": text})
                                        elif t == "force_keyframe":
                                            self.streamer.request_force_keyframe()
                                        else:
                                            self._handle_input(event)
                                    except json.JSONDecodeError:
                                        pass
                                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                                  aiohttp.WSMsgType.ERROR):
                                    break
                        finally:
                            stream_task.cancel()
                            self.streamer.remove_client(want_jpeg[0])
                            print(f"  📴  Relay uplink: phone disconnected or relay closed")

            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"  ⚠️  Relay uplink error: {e}")

            print(f"  🔄  Relay uplink: reconnecting in 3s...")
            await asyncio.sleep(3)

    async def _index_handler(self, request):
        return web.FileResponse(os.path.join(self.static_dir, "index.html"))

    async def _wol_handler(self, request):
        """POST /wol — {"mac": "AA:BB:CC:DD:EE:FF"}"""
        try:
            data = await request.json()
            mac = data.get("mac", "")
            if not mac:
                return web.json_response({"error": "mac required"}, status=400)
            from shared.wol import send_magic_packet
            send_magic_packet(mac)
            return web.json_response({"status": "sent", "mac": mac})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def _ws_handler(self, request):
        # ── Token authentication ──────────────────────────────────────
        if request.rel_url.query.get("token", "") != self.token:
            raise web.HTTPForbidden(reason="Invalid or missing token")

        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        client_ip = request.remote
        print(f"  📱  Client connected: {client_ip}")

        want_jpeg = [self.streamer.h264_encoder is None]
        # Register client for conditional encoding
        self.streamer.add_client(want_jpeg[0])

        async def _send_config(codec_override=None):
            codec = codec_override or self.streamer.codec_name
            await ws.send_json({
                "type":   "config",
                "width":  self.streamer.width,
                "height": self.streamer.height,
                "fps":    self.args.fps,
                "codec":  codec,
                "audio":  self.audio.available,
            })

        await _send_config()

        stream_task = asyncio.create_task(self._stream_to_client(ws, want_jpeg))
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        event = json.loads(msg.data)
                        t = event.get("type")
                        if t == "ping":
                            await ws.send_json({"type": "pong", "ts": event.get("ts")})
                        elif t == "codec_pref":
                            old_want_jpeg = want_jpeg[0]
                            new_want_jpeg = (event.get("codec") == "jpeg")
                            if old_want_jpeg != new_want_jpeg:
                                self.streamer.update_client_codec(old_want_jpeg, new_want_jpeg)
                                want_jpeg[0] = new_want_jpeg
                            codec = "jpeg" if want_jpeg[0] else self.streamer.codec_name
                            print(f"  📱  {client_ip} codec preference: {codec}")
                            await _send_config(codec_override=codec)
                        elif t == "wol":
                            mac = event.get("mac", "")
                            if mac:
                                try:
                                    from shared.wol import send_magic_packet
                                    send_magic_packet(mac)
                                    await ws.send_json({"type": "wol_result", "status": "sent"})
                                except Exception as e:
                                    await ws.send_json({"type": "wol_result", "error": str(e)})
                        elif t == "clipboard_push":
                            text = event.get("text", "")
                            if HAS_CLIPBOARD:
                                try:
                                    pyperclip.copy(text)
                                    print(f"  📋  Clipboard received from mobile ({len(text)} chars)")
                                except Exception as e:
                                    print(f"  ⚠️  Clipboard write failed: {e}")
                        elif t == "clipboard_pull":
                            text = ""
                            if HAS_CLIPBOARD:
                                try:
                                    text = pyperclip.paste()
                                except Exception:
                                    pass
                            await ws.send_json({"type": "clipboard", "text": text})
                        elif t == "force_keyframe":
                            self.streamer.request_force_keyframe()
                        else:
                            self._handle_input(event)
                    except json.JSONDecodeError:
                        pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        finally:
            stream_task.cancel()
            self.streamer.remove_client(want_jpeg[0])
            print(f"  📴  Client disconnected: {client_ip}")

        return ws

    async def _stream_to_client(self, ws: web.WebSocketResponse, want_jpeg: list):
        """
        Flow-controlled sender for video + audio.

        want_jpeg is a [bool] list so it can be updated live from the WS
        message handler (client can request JPEG fallback mid-stream).
        """
        video_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        audio_q: asyncio.Queue | None = (
            self.audio.subscribe() if self.audio.available else None
        )

        async def _produce_video():
            try:
                while not ws.closed:
                    frame = self.streamer.get_frame(want_jpeg=want_jpeg[0])
                    if frame:
                        if video_q.full():
                            try:
                                video_q.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                        try:
                            video_q.put_nowait(frame)
                        except asyncio.QueueFull:
                            pass
                    await asyncio.sleep(1.0 / max(self.streamer.max_fps, 1))
            except (asyncio.CancelledError, Exception):
                pass

        producer = asyncio.create_task(_produce_video())
        try:
            while not ws.closed:
                if audio_q:
                    for _ in range(8):
                        try:
                            pkt = audio_q.get_nowait()
                            await ws.send_bytes(pkt)
                        except asyncio.QueueEmpty:
                            break
                        except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                            return

                try:
                    frame = await asyncio.wait_for(video_q.get(), timeout=0.2)
                    await ws.send_bytes(frame)
                except asyncio.TimeoutError:
                    pass
                except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                    return
        except (asyncio.CancelledError, ConnectionResetError,
                aiohttp.ClientConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            producer.cancel()
            if audio_q:
                self.audio.unsubscribe(audio_q)

    def _handle_input(self, event: dict):
        t = event.get("type", "")
        if t == "quality":
            self.streamer.set_quality(event.get("value", 60))
        elif t == "fps":
            new_fps = max(10, min(60, event.get("value", 30)))
            self.streamer.max_fps = new_fps
            self.streamer._kf_every = new_fps * 2
        else:
            self.input_handler.handle(event)


def main():
    p = argparse.ArgumentParser(description="GameStream Mobile Gateway v2")
    p.add_argument("--port",     type=int,   default=8080,  help="HTTP port")
    p.add_argument("--tls",      action="store_true",        help="Enable TLS")
    p.add_argument("--tls-port", type=int,   default=8443,  help="HTTPS port")
    p.add_argument("--fps",      type=int,   default=30,    help="Max FPS")
    p.add_argument("--quality",  type=int,   default=55,    help="JPEG quality (fallback)")
    p.add_argument("--monitor",  type=int,   default=0,     help="Monitor index")
    p.add_argument("--scale",    type=float, default=0.75,  help="Resolution scale 0.5–1.0")
    p.add_argument("--no-h264",  action="store_true",        help="Force JPEG (old browsers)")
    p.add_argument("--relay",    type=str,   default=None,
                   help="Relay server host:port (HTTP relay for gateway<->phone)")
    p.add_argument("--room",     type=str,   default=None,
                   help="Relay room code (4-char hex); auto-generated if omitted)")
    args = p.parse_args()

    asyncio.run(MobileGateway(args).start())


if __name__ == "__main__":
    main()
