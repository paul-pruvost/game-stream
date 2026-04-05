"""
GameStream Audio — System audio capture, Opus encoding, and playback.

Capture methods:
  - Windows: WASAPI loopback (captures all system audio)
  - Linux:   PulseAudio monitor source
  - macOS:   BlackHole or Soundflower virtual device (manual setup)

Encoding:
  - Opus (preferred) — excellent quality at low bitrate (~128kbps)
  - Raw PCM fallback — uncompressed, higher bandwidth

Playback:
  - Cross-platform via sounddevice (PortAudio backend)
"""

import threading
import time
import struct
import sys
import queue
from typing import Optional, Callable

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    import sounddevice as sd
    HAS_SD = True
except ImportError:
    HAS_SD = False

# Opus support via opuslib or av
HAS_OPUS = False
OPUS_BACKEND = None
try:
    import av
    # Test that we can create an opus codec
    _test = av.codec.Codec("libopus", "w")
    HAS_OPUS = True
    OPUS_BACKEND = "av"
except Exception:
    pass

if not HAS_OPUS:
    try:
        import opuslib
        HAS_OPUS = True
        OPUS_BACKEND = "opuslib"
    except ImportError:
        pass


# ── Constants ──────────────────────────────────────────────────────────
SAMPLE_RATE     = 48000          # Opus native rate
CHANNELS        = 2              # Stereo
FRAME_DURATION  = 20             # ms per audio frame (Opus supports 2.5–60ms)
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_DURATION // 1000   # 960 samples
OPUS_BITRATE    = 128_000        # 128 kbps Opus


# ══════════════════════════════════════════════════════════════════════
#  Audio Capture (Host side)
# ══════════════════════════════════════════════════════════════════════

class AudioCapture:
    """
    Captures system audio (loopback) for streaming.

    On Windows, uses WASAPI loopback.
    On Linux, auto-detects PulseAudio monitor source.
    """

    def __init__(self, device: Optional[int] = None, sample_rate: int = SAMPLE_RATE):
        if not HAS_SD:
            raise RuntimeError("sounddevice not installed. pip install sounddevice")

        self.sample_rate = sample_rate
        self.channels = CHANNELS
        self.running = False
        self._buffer = queue.Queue(maxsize=200)
        self._device = device
        self._stream = None

        # Auto-detect loopback device
        if self._device is None:
            self._device = self._find_loopback()

        if self._device is None:
            raise RuntimeError(
                "No audio loopback device found. Enable 'Stereo Mix' in Windows Sound settings "
                "(Recording → right-click → Show Disabled Devices → enable Stereo Mix), "
                "or install a virtual audio device."
            )

    def _find_loopback(self) -> Optional[int]:
        """
        Find the best loopback/monitor audio device for system audio capture.

        On Windows, WASAPI loopback is the most reliable approach. We search
        specifically within the WASAPI host API to avoid picking up MME/DS
        devices that may share the same name without loopback support.
        """
        devices = sd.query_devices()
        platform = sys.platform

        if platform == "win32":
            # ── Strategy 1: WASAPI host API loopback devices ──────────
            # With sounddevice + PortAudio WASAPI, loopback devices appear
            # as input devices named "<device> (loopback)".
            # Filter by WASAPI host API for reliability.
            try:
                apis = sd.query_hostapis()
                wasapi_idx = next(
                    (i for i, a in enumerate(apis) if "WASAPI" in a["name"]),
                    None
                )
            except Exception:
                wasapi_idx = None

            for i, dev in enumerate(devices):
                name = dev["name"].lower()
                if dev.get("max_input_channels", 0) < 1:
                    continue
                # Prefer WASAPI loopback devices
                is_wasapi = (wasapi_idx is not None and
                             dev.get("hostapi") == wasapi_idx)
                if is_wasapi and "loopback" in name:
                    print(f"  🔊  WASAPI loopback: [{i}] {dev['name']}")
                    return i

            # ── Strategy 2: Any device with loopback/stereo mix ───────
            for i, dev in enumerate(devices):
                name = dev["name"].lower()
                if dev.get("max_input_channels", 0) < 1:
                    continue
                if "loopback" in name or "stereo mix" in name:
                    print(f"  🔊  Audio loopback: [{i}] {dev['name']}")
                    return i

            # ── Strategy 3: WASAPI loopback of default output device ──
            # PortAudio/WASAPI exposes the default output device as a
            # virtual loopback input when opened with the same index.
            if wasapi_idx is not None:
                try:
                    default_out = sd.default.device[1]
                    if default_out >= 0:
                        out_dev = sd.query_devices(default_out)
                        # Verify it's a WASAPI output device
                        if out_dev.get("hostapi") == wasapi_idx:
                            # Test that opening it as input works
                            sd.check_input_settings(
                                device=default_out,
                                samplerate=SAMPLE_RATE,
                                channels=CHANNELS,
                                dtype="float32",
                            )
                            print(f"  🔊  WASAPI loopback (default output): "
                                  f"[{default_out}] {out_dev['name']}")
                            return default_out
                except Exception:
                    pass

            print(f"  ⚠️  No WASAPI loopback found — audio capture disabled.")
            print(f"       Fix: right-click the speaker icon → Sounds → Recording")
            print(f"       → right-click empty area → Show Disabled Devices")
            print(f"       → enable 'Stereo Mix'  (or install dxcam for video fix)")
            return None

        elif platform == "linux":
            for i, dev in enumerate(devices):
                if dev.get("max_input_channels", 0) < 1:
                    continue
                if "monitor" in dev["name"].lower():
                    print(f"  🔊  PulseAudio monitor: [{i}] {dev['name']}")
                    return i
            print(f"  ⚠️  No monitor source found.")
            print(f"       Fix: pactl load-module module-loopback")
            return None

        elif platform == "darwin":
            for i, dev in enumerate(devices):
                if dev.get("max_input_channels", 0) < 1:
                    continue
                name = dev["name"].lower()
                if "blackhole" in name or "soundflower" in name:
                    print(f"  🔊  Virtual audio: [{i}] {dev['name']}")
                    return i
            print(f"  ⚠️  No virtual audio device found.")
            print(f"       Fix: brew install blackhole-2ch")
            return None

        # Unknown platform — use default input
        return None

    def start(self):
        """Start capturing audio."""
        self.running = True

        def _callback(indata, frames, time_info, status):
            if status:
                pass  # Silently ignore xruns
            if self.running:
                # Copy float32 audio data
                try:
                    self._buffer.put_nowait(indata.copy())
                except queue.Full:
                    pass  # Drop oldest frames

        try:
            self._stream = sd.InputStream(
                device=self._device,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=SAMPLES_PER_FRAME,
                callback=_callback,
            )
            self._stream.start()
            print(f"  🎙️   Audio capture started (rate={self.sample_rate}, ch={self.channels})")
        except Exception as e:
            print(f"  ⚠️  Audio capture failed: {e}")
            self.running = False

    def read(self) -> Optional[np.ndarray]:
        """Read one audio frame (blocks briefly). Returns float32 array or None."""
        try:
            return self._buffer.get(timeout=0.05)
        except queue.Empty:
            return None

    def stop(self):
        self.running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except:
                pass

    @staticmethod
    def list_devices():
        """Print available audio devices for debugging."""
        if not HAS_SD:
            print("sounddevice not installed")
            return
        print("\n  Available audio devices:")
        devices = sd.query_devices()
        for i, dev in enumerate(devices):
            direction = ""
            if dev.get("max_input_channels", 0) > 0:
                direction += "IN"
            if dev.get("max_output_channels", 0) > 0:
                direction += "/OUT" if direction else "OUT"
            print(f"    [{i:2d}] {dev['name']:<50} ({direction})")
        print()


# ══════════════════════════════════════════════════════════════════════
#  Audio Playback (Client side)
# ══════════════════════════════════════════════════════════════════════

class AudioPlayer:
    """Plays back received audio on the client."""

    def __init__(self, device: Optional[int] = None, sample_rate: int = SAMPLE_RATE,
                 buffer_ms: int = 60):
        if not HAS_SD:
            raise RuntimeError("sounddevice not installed. pip install sounddevice")

        self.sample_rate = sample_rate
        self.channels = CHANNELS
        self.running = False
        self._device = device
        self._stream = None

        # Jitter buffer: holds audio frames for smooth playback
        buffer_frames = max(2, buffer_ms // FRAME_DURATION)
        self._buffer = queue.Queue(maxsize=buffer_frames * 3)
        self._silence = np.zeros((SAMPLES_PER_FRAME, CHANNELS), dtype="float32")

    def start(self):
        self.running = True

        def _callback(outdata, frames, time_info, status):
            try:
                data = self._buffer.get_nowait()
                # Ensure correct shape
                if data.shape[0] >= frames:
                    outdata[:] = data[:frames]
                else:
                    outdata[:data.shape[0]] = data
                    outdata[data.shape[0]:] = 0
            except queue.Empty:
                outdata[:] = 0  # Silence on underrun

        try:
            self._stream = sd.OutputStream(
                device=self._device,
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="float32",
                blocksize=SAMPLES_PER_FRAME,
                callback=_callback,
            )
            self._stream.start()
            print(f"  🔈  Audio playback started")
        except Exception as e:
            print(f"  ⚠️  Audio playback failed: {e}")
            self.running = False

    def write(self, audio_data: np.ndarray):
        """Queue audio data for playback."""
        try:
            self._buffer.put_nowait(audio_data)
        except queue.Full:
            # Drop oldest to keep latency low
            try:
                self._buffer.get_nowait()
                self._buffer.put_nowait(audio_data)
            except:
                pass

    def stop(self):
        self.running = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except:
                pass


# ══════════════════════════════════════════════════════════════════════
#  Opus Encoder / Decoder
# ══════════════════════════════════════════════════════════════════════

class OpusEncoder:
    """Encodes float32 PCM frames to Opus using PyAV or opuslib."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS,
                 bitrate: int = OPUS_BITRATE):
        self.sample_rate = sample_rate
        self.channels = channels
        self.backend = OPUS_BACKEND

        if self.backend == "av":
            import av as _av
            codec = _av.codec.Codec("libopus", "w")
            self.ctx = _av.codec.CodecContext.create(codec)
            self.ctx.sample_rate = sample_rate
            self.ctx.channels = channels
            self.ctx.format = _av.AudioFormat("fltp")
            self.ctx.bit_rate = bitrate
            from fractions import Fraction as _Fraction
            self.ctx.time_base = _Fraction(1, sample_rate)
            self.ctx.options = {"frame_duration": str(FRAME_DURATION)}
            self.ctx.open()
            self._pts = 0

        elif self.backend == "opuslib":
            import opuslib as _opus
            self.enc = _opus.Encoder(sample_rate, channels, opuslib.APPLICATION_AUDIO)
            self.enc.bitrate = bitrate

        else:
            # No Opus — will send raw PCM
            self.backend = None

        if self.backend:
            print(f"  🎵  Audio encoder: Opus ({self.backend}) @ {bitrate//1000}kbps")
        else:
            print(f"  🎵  Audio encoder: Raw PCM (Opus not available)")

    def encode(self, pcm_float32: np.ndarray) -> bytes:
        """Encode one PCM frame to Opus. Input: float32 (samples, channels)."""
        if self.backend == "av":
            import av as _av
            # Convert to planar float
            frame = _av.AudioFrame.from_ndarray(
                pcm_float32.T.astype(np.float32),
                format="fltp",
                layout="stereo" if self.channels == 2 else "mono"
            )
            frame.sample_rate = self.sample_rate
            frame.pts = self._pts
            self._pts += pcm_float32.shape[0]

            packets = self.ctx.encode(frame)
            data = bytearray()
            for pkt in packets:
                data.extend(bytes(pkt))
            return bytes(data)

        elif self.backend == "opuslib":
            # Convert float32 to int16 for opuslib
            pcm_int16 = (pcm_float32 * 32767).astype(np.int16)
            return self.enc.encode(pcm_int16.tobytes(), SAMPLES_PER_FRAME)

        else:
            # Raw PCM as bytes
            return pcm_float32.astype(np.float32).tobytes()

    def close(self):
        if self.backend == "av" and hasattr(self, 'ctx'):
            try:
                self.ctx.close()
            except:
                pass


class OpusDecoder:
    """Decodes Opus packets back to float32 PCM."""

    def __init__(self, sample_rate: int = SAMPLE_RATE, channels: int = CHANNELS):
        self.sample_rate = sample_rate
        self.channels = channels
        self.backend = OPUS_BACKEND

        if self.backend == "av":
            import av as _av
            codec = _av.codec.Codec("opus", "r")
            self.ctx = _av.codec.CodecContext.create(codec)
            self.ctx.sample_rate = sample_rate
            self.ctx.channels = channels
            self.ctx.open()

        elif self.backend == "opuslib":
            import opuslib as _opus
            self.dec = _opus.Decoder(sample_rate, channels)

        else:
            self.backend = None

    def decode(self, data: bytes) -> Optional[np.ndarray]:
        """Decode Opus packet to float32 (samples, channels)."""
        try:
            if self.backend == "av":
                import av as _av
                pkt = _av.Packet(data)
                frames = self.ctx.decode(pkt)
                for frame in frames:
                    arr = frame.to_ndarray()
                    # Planar to interleaved
                    if arr.ndim == 2:
                        return arr.T.astype(np.float32)
                    return arr.astype(np.float32).reshape(-1, self.channels)

            elif self.backend == "opuslib":
                pcm_bytes = self.dec.decode(data, SAMPLES_PER_FRAME)
                pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
                pcm_float = pcm_int16.astype(np.float32) / 32767.0
                return pcm_float.reshape(-1, self.channels)

            else:
                # Raw PCM
                pcm = np.frombuffer(data, dtype=np.float32)
                return pcm.reshape(-1, self.channels)

        except Exception:
            return None

    def close(self):
        if self.backend == "av" and hasattr(self, 'ctx'):
            try:
                self.ctx.close()
            except:
                pass


# ══════════════════════════════════════════════════════════════════════
#  Factory
# ══════════════════════════════════════════════════════════════════════

def create_audio_encoder(sample_rate=SAMPLE_RATE, channels=CHANNELS, bitrate=OPUS_BITRATE):
    return OpusEncoder(sample_rate, channels, bitrate)

def create_audio_decoder(sample_rate=SAMPLE_RATE, channels=CHANNELS):
    return OpusDecoder(sample_rate, channels)

def audio_available() -> bool:
    return HAS_SD and HAS_NUMPY
