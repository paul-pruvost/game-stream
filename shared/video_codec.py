"""
GameStream Video Codec — H.264 encoding/decoding via PyAV (FFmpeg).

Supports:
  - Software encoding (libx264) — universal fallback
  - NVIDIA hardware encoding (h264_nvenc) — GeForce/Quadro GPUs
  - AMD hardware encoding (h264_amf) — Radeon GPUs
  - Intel hardware encoding (h264_qsv) — Intel iGPU / Arc

Encoder produces NAL units that are chunked and sent over UDP.
Decoder reassembles and decodes back to RGB frames.

Tuned for low latency:
  - zerolatency tune
  - No B-frames
  - Short GOP (keyframe every 2s)
  - CBR or CRF with vbv constraints
"""

import time
import numpy as np
from fractions import Fraction
from typing import Optional, Tuple
from enum import Enum

try:
    import av
    HAS_AV = True
except ImportError:
    HAS_AV = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


class HWAccel(Enum):
    NONE   = "libx264"
    NVENC  = "h264_nvenc"
    AMF    = "h264_amf"
    QSV    = "h264_qsv"


def detect_hw_encoder() -> str:
    """
    Probe available hardware encoders by actually opening a codec context.
    Just checking codec existence is not enough — the codec may exist in
    FFmpeg but fail to open if the driver or SDK is missing/incompatible.
    """
    if not HAS_AV:
        return HWAccel.NONE.value

    # Priority: NVENC > QSV > AMF > software
    for accel in [HWAccel.NVENC, HWAccel.QSV, HWAccel.AMF]:
        try:
            codec = av.codec.Codec(accel.value, "w")
            # Actually open a minimal context to verify driver/SDK works
            ctx = av.codec.CodecContext.create(codec)
            ctx.width = 64
            ctx.height = 64
            ctx.pix_fmt = "yuv420p"
            ctx.time_base = Fraction(1, 30)
            ctx.framerate = Fraction(30, 1)
            ctx.open()
            ctx.close()
            return accel.value
        except Exception:
            continue
    return HWAccel.NONE.value


class H264Encoder:
    """
    Low-latency H.264 encoder using PyAV.

    Produces encoded byte packets for each input frame.
    Automatically inserts keyframes at the configured interval.
    """

    def __init__(
        self,
        width: int,
        height: int,
        fps: int = 60,
        bitrate: int = 8_000_000,       # 8 Mbps default
        keyframe_interval: int = 60,    # Keyframe every 1s at 60fps
        codec_name: Optional[str] = None,
        preset: str = "ultrafast",
        crf: Optional[int] = None,
    ):
        if not HAS_AV:
            raise RuntimeError("PyAV not installed. pip install av")

        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = 0

        # Auto-detect or use specified codec
        if codec_name is None:
            codec_name = detect_hw_encoder()

        self.codec_name = codec_name
        print(f"  🎬  Video encoder: {codec_name} @ {width}x{height} {fps}fps")

        # Create codec context
        codec = av.codec.Codec(codec_name, "w")
        self.ctx = av.codec.CodecContext.create(codec)
        self.ctx.width = width
        self.ctx.height = height
        self.ctx.time_base = Fraction(1, fps)
        self.ctx.framerate = Fraction(fps, 1)
        self.ctx.pix_fmt = "yuv420p"
        self.ctx.gop_size = keyframe_interval
        self.ctx.max_b_frames = 0      # No B-frames for low latency
        self.ctx.thread_count = 0      # Auto thread count

        # Codec-specific options
        if codec_name == "libx264":
            self.ctx.bit_rate = bitrate
            opts = {
                "preset": preset,
                "tune": "zerolatency",
                "profile": "baseline",    # Widest compatibility
            }
            if crf is not None:
                opts["crf"] = str(crf)
                # With CRF, constrain with VBV for stable bitrate
                opts["maxrate"] = str(bitrate)
                opts["bufsize"] = str(bitrate // 2)
            self.ctx.options = opts

        elif codec_name == "h264_nvenc":
            self.ctx.bit_rate = bitrate
            self.ctx.options = {
                "preset": "p1",            # Fastest NVENC preset
                "tune": "ull",             # Ultra low latency
                "profile": "baseline",
                "rc": "cbr",
                "delay": "0",
                "zerolatency": "1",
            }

        elif codec_name == "h264_amf":
            self.ctx.bit_rate = bitrate
            self.ctx.options = {
                "usage": "ultralowlatency",
                "profile": "baseline",
                "rc": "cbr",
                "quality": "speed",
            }

        elif codec_name == "h264_qsv":
            self.ctx.bit_rate = bitrate
            self.ctx.options = {
                "preset": "veryfast",
                "profile": "baseline",
                "low_power": "1",
            }

        self.ctx.open()
        self._sws = None      # Will be created on first frame

    def encode(self, frame_rgb: np.ndarray, force_keyframe: bool = False) -> bytes:
        """
        Encode a single RGB frame to H.264 bytes.

        Args:
            frame_rgb: numpy array (H, W, 3) in RGB format
            force_keyframe: Force an IDR keyframe

        Returns:
            Encoded bytes (may be empty if encoder is buffering)
        """
        # Convert numpy RGB to av.VideoFrame
        h, w = frame_rgb.shape[:2]
        frame = av.VideoFrame.from_ndarray(frame_rgb, format="rgb24")
        frame.pts = self.frame_count
        frame.time_base = self.ctx.time_base

        if force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I

        self.frame_count += 1

        # Encode
        packets = self.ctx.encode(frame)
        output = bytearray()
        for pkt in packets:
            output.extend(bytes(pkt))
        return bytes(output)

    def flush(self) -> bytes:
        """Flush remaining buffered frames."""
        packets = self.ctx.encode(None)
        output = bytearray()
        for pkt in packets:
            output.extend(bytes(pkt))
        return bytes(output)

    def set_bitrate(self, bitrate: int):
        """Dynamically adjust bitrate (best effort, not all codecs support this)."""
        try:
            self.ctx.bit_rate = bitrate
        except Exception:
            pass

    def close(self):
        if self.ctx:
            self.ctx.close()


class H264Decoder:
    """
    H.264 decoder using PyAV.

    Accepts encoded byte packets and outputs RGB numpy arrays.
    Handles stream corruption gracefully with automatic recovery.
    """

    def __init__(self):
        if not HAS_AV:
            raise RuntimeError("PyAV not installed. pip install av")

        self._init_decoder()
        self.frames_decoded = 0
        self.errors = 0

    def _init_decoder(self):
        """Initialize or re-initialize the decoder."""
        codec = av.codec.Codec("h264", "r")
        self.ctx = av.codec.CodecContext.create(codec)
        self.ctx.thread_count = 0
        self.ctx.options = {"flags2": "+showall"}  # Show all frames
        self.ctx.open()
        self._buffer = bytearray()

    def decode(self, data: bytes) -> Optional[np.ndarray]:
        """
        Decode H.264 encoded data to an RGB numpy array.

        Args:
            data: Encoded H.264 bytes (one or more NAL units)

        Returns:
            RGB numpy array (H, W, 3) or None if no frame produced
        """
        try:
            packet = av.Packet(data)
            frames = self.ctx.decode(packet)
            for frame in frames:
                self.frames_decoded += 1
                return frame.to_ndarray(format="rgb24")
        except av.error.InvalidDataError:
            self.errors += 1
            # Attempt recovery by re-initializing
            if self.errors % 10 == 0:
                try:
                    self.ctx.close()
                except:
                    pass
                self._init_decoder()
        except Exception:
            self.errors += 1
        return None

    def close(self):
        if self.ctx:
            try:
                self.ctx.close()
            except:
                pass


# ══════════════════════════════════════════════════════════════════════
#  MJPEG Fallback (when PyAV is not available)
# ══════════════════════════════════════════════════════════════════════

class MJPEGEncoder:
    """Fallback JPEG-per-frame encoder using OpenCV."""

    def __init__(self, quality: int = 65):
        if not HAS_CV2:
            raise RuntimeError("OpenCV not installed. pip install opencv-python")
        self.quality = quality
        self.codec_name = "mjpeg"
        print(f"  🎬  Video encoder: MJPEG (fallback) quality={quality}")

    def encode(self, frame_rgb: np.ndarray, force_keyframe: bool = False) -> bytes:
        bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        _, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        return buf.tobytes()

    def flush(self) -> bytes:
        return b""

    def set_bitrate(self, bitrate: int):
        # Approximate: map bitrate to quality
        self.quality = max(20, min(95, bitrate // 100_000))

    def close(self):
        pass


class MJPEGDecoder:
    """Fallback JPEG decoder using OpenCV."""

    def __init__(self):
        if not HAS_CV2:
            raise RuntimeError("OpenCV not installed. pip install opencv-python")
        self.frames_decoded = 0
        self.errors = 0
        self.codec_name = "mjpeg"

    def decode(self, data: bytes) -> Optional[np.ndarray]:
        try:
            arr = np.frombuffer(data, dtype=np.uint8)
            bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if bgr is not None:
                self.frames_decoded += 1
                return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        except Exception:
            self.errors += 1
        return None

    def close(self):
        pass


# ══════════════════════════════════════════════════════════════════════
#  Factory functions
# ══════════════════════════════════════════════════════════════════════

def create_encoder(
    width: int,
    height: int,
    fps: int = 60,
    bitrate: int = 8_000_000,
    prefer_hw: bool = True,
    fallback_quality: int = 65,
    **kwargs
):
    """Create the best available encoder."""
    if HAS_AV:
        # Try hardware encoder first (if requested), then libx264, then MJPEG
        codecs_to_try = []
        if prefer_hw:
            hw = detect_hw_encoder()
            if hw != HWAccel.NONE.value:
                codecs_to_try.append(hw)
        codecs_to_try.append(HWAccel.NONE.value)  # libx264 software fallback

        for codec_name in codecs_to_try:
            try:
                return H264Encoder(width, height, fps, bitrate, codec_name=codec_name, **kwargs)
            except Exception as e:
                print(f"  ⚠️  H.264 unavailable ({e}), trying next encoder...")

    return MJPEGEncoder(quality=fallback_quality)


def create_decoder(codec_name: str = "h264"):
    """Create a decoder matching the encoder's codec."""
    if codec_name in ("h264", "libx264", "h264_nvenc", "h264_amf", "h264_qsv"):
        if HAS_AV:
            try:
                return H264Decoder()
            except Exception as e:
                print(f"  ⚠️  H.264 decoder failed ({e}), falling back to MJPEG")
    return MJPEGDecoder()
