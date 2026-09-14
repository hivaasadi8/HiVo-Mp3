"""
HiVo Converter Layer
--------------------
لایه تبدیل ویدیو به صدا با FFmpeg.

ویژگی‌ها:
  • تشخیص خودکار کدک‌های موجود در FFmpeg
  • پروب کامل متادیتا (title/artist/album/year/genre/track)
  • استخراج کاور با چندین fallback (ثانیه‌های مختلف)
  • نرمال‌سازی صدا (EBU R128 loudnorm)
  • حذف سکوت ابتدا/انتها
  • پشتیبانی از پیش‌فرض‌های حرفه‌ای (128/192/320/V0)
  • تخمین حجم خروجی قبل از تبدیل
  • مدیریت تمیز فایل‌های temp
  • خطاهای ساختاریافته با پیام فارسی
  • Timeout و retry قابل تنظیم
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("HiVo.Converter")

# --------------------------------------------------------------------------- #
# Binary discovery
# --------------------------------------------------------------------------- #

FFMPEG  = shutil.which("ffmpeg")  or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

_DEFAULT_TIMEOUT = 1800   # 30 دقیقه
_THUMB_TIMEOUT   = 60
_PROBE_TIMEOUT   = 30

# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #

# (bitrate, sample_rate, channels, codec_extra)
PRESETS: Dict[str, Dict[str, Any]] = {
    # MP3
    "mp3_128":  {"fmt": "mp3", "bitrate": "128k", "codec": "libmp3lame"},
    "mp3_192":  {"fmt": "mp3", "bitrate": "192k", "codec": "libmp3lame"},
    "mp3_320":  {"fmt": "mp3", "bitrate": "320k", "codec": "libmp3lame"},
    "mp3_v0":   {"fmt": "mp3", "bitrate": None,   "codec": "libmp3lame",
                 "extra": ["-q:a", "0"]},  # VBR V0 = بهترین کیفیت MP3
    # M4A / AAC
    "m4a_128":  {"fmt": "m4a", "bitrate": "128k", "codec": "aac"},
    "m4a_192":  {"fmt": "m4a", "bitrate": "192k", "codec": "aac"},
    "m4a_256":  {"fmt": "m4a", "bitrate": "256k", "codec": "aac"},
    # OGG / Opus (برای ویس تلگرام)
    "voice":    {"fmt": "voice", "bitrate": "48k", "codec": "libopus",
                 "extra": ["-ar", "48000", "-ac", "1", "-application", "voip"]},
    # FLAC (بدون افت)
    "flac":     {"fmt": "flac", "bitrate": None, "codec": "flac",
                 "extra": ["-compression_level", "8"]},
    # WAV
    "wav":      {"fmt": "wav", "bitrate": None, "codec": "pcm_s16le"},
}

_EXT_MAP = {
    "mp3": "mp3", "m4a": "m4a", "voice": "ogg",
    "flac": "flac", "wav": "wav",
}

# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #

class ConvertError(Exception):
    """پایه خطاهای تبدیل."""

class FFmpegNotFound(ConvertError):
    """FFmpeg نصب نیست."""

class FFmpegFailed(ConvertError):
    """FFmpeg با خطا برگشت."""
    def __init__(self, msg: str, stderr: str = ""):
        super().__init__(msg)
        self.stderr = stderr

class InputError(ConvertError):
    """فایل ورودی معتبر نیست."""

class TimeoutError_(ConvertError):
    """عملیات timeout خورد."""

# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #

@dataclass
class MediaInfo:
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    has_video: bool = False
    has_audio: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0
    sample_rate: int = 0
    channels: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    track: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_str(self) -> str:
        d = int(self.duration)
        h, rem = divmod(d, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class ConvertResult:
    path: str
    fmt: str
    bitrate: str
    size: int
    duration: float
    elapsed: float

    @property
    def size_mb(self) -> float:
        return self.size / (1024 * 1024)


# --------------------------------------------------------------------------- #
# FFmpeg capability detection
# --------------------------------------------------------------------------- #

_CAP_CACHE: Optional[Dict[str, bool]] = None


def ffmpeg_available() -> bool:
    """چک می‌کنه FFmpeg نصب هست یا نه."""
    try:
        subprocess.run([FFMPEG, "-version"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10)
        return True
    except Exception:
        return False


def ffmpeg_capabilities(force: bool = False) -> Dict[str, bool]:
    """
    تشخیص کدک‌های موجود.
    خروجی مثل: {"libmp3lame": True, "aac": True, "libopus": True, "flac": True}
    """
    global _CAP_CACHE
    if _CAP_CACHE is not None and not force:
        return _CAP_CACHE

    wanted = ["libmp3lame", "aac", "libopus", "flac", "pcm_s16le", "libvorbis"]
    caps = {k: False for k in wanted}

    try:
        out = subprocess.check_output(
            [FFMPEG, "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT, timeout=15,
        ).decode("utf-8", errors="ignore")
        for codec in wanted:
            caps[codec] = codec in out
    except Exception as e:
        LOG.warning("capability detection failed: %s", e)

    _CAP_CACHE = caps
    return caps


def assert_ffmpeg() -> None:
    if not ffmpeg_available():
        raise FFmpegNotFound(
            "FFmpeg در سیستم نصب نیست. لطفاً نصب کن: sudo apt-get install -y ffmpeg"
        )


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #

def _run_probe(path: str, timeout: int = _PROBE_TIMEOUT) -> dict:
    cmd = [
        FFPROBE, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    out = subprocess.check_output(cmd, timeout=timeout,
                                  stderr=subprocess.DEVNULL)
    return json.loads(out.decode("utf-8", errors="ignore"))


def probe(path: str) -> MediaInfo:
    """
    پروب کامل فایل — مدت، کدک، رزولوشن، متادیتا، و غیره.
    """
    if not os.path.exists(path):
        raise InputError(f"فایل پیدا نشد: {path}")

    try:
        data = _run_probe(path)
    except subprocess.CalledProcessError as e:
        raise InputError(f"ffprobe خطا داد: {e}")
    except Exception as e:
        raise InputError(f"خواندن فایل ناموفق: {e}")

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    info = MediaInfo()
    info.duration = float(fmt.get("duration") or 0.0)
    info.size = int(fmt.get("size") or 0)
    info.bitrate = int(fmt.get("bit_rate") or 0)

    # متادیتا (سطح format)
    info.title  = tags.get("title", "")
    info.artist = tags.get("artist", "") or tags.get("album_artist", "")
    info.album  = tags.get("album", "")
    info.year   = tags.get("date", "")[:4] or tags.get("year", "")
    info.genre  = tags.get("genre", "")
    info.track  = tags.get("track", "")

    for st in streams:
        codec_type = st.get("codec_type")
        st_tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}

        if codec_type == "video" and st.get("disposition", {}).get("attached_pic") != 1:
            info.has_video = True
            info.video_codec = st.get("codec_name", "")
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)

        elif codec_type == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = st.get("codec_name", "")
            info.sample_rate = int(st.get("sample_rate") or 0)
            info.channels = int(st.get("channels") or 0)

            # متادیتا سطح stream (اولویت داره اگه خالی بود)
            info.title  = info.title  or st_tags.get("title", "")
            info.artist = info.artist or st_tags.get("artist", "")
            info.album  = info.album  or st_tags.get("album", "")

    info.raw = data
    return info


def probe_duration(path: str) -> float:
    """نسخه سبک — فقط مدت."""
    try:
        return probe(path).duration
    except Exception:
        return 0.0


def is_valid_media(path: str) -> bool:
    """چک سریع که فایل رسانه‌ای معتبره."""
    try:
        info = probe(path)
        return info.has_audio or info.has_video
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

def _temp_out(ext: str) -> str:
    return os.path.join(tempfile.gettempdir(),
                        f"hivo_{uuid.uuid4().hex}.{ext}")


def estimate_size(duration: float, bitrate: str) -> int:
    """تخمین حجم خروجی به بایت."""
    m = re.match(r"(\d+)k", bitrate or "")
    if not m or duration <= 0:
        return 0
    kbps = int(m.group(1))
    return int(kbps * 1000 * duration / 8)


# --------------------------------------------------------------------------- #
# Thumbnail extraction
# --------------------------------------------------------------------------- #

def extract_thumbnail(src: str, at: float = 1.0, size: int = 320) -> Optional[str]:
    """
    یه فریم از ویدیو استخراج می‌کنه.
    اگه ثانیه‌ی داده‌شده کار نکرد، از چندین نقطه fallback می‌کنه.
    """
    info = None
    try:
        info = probe(src)
    except Exception:
        pass

    duration = info.duration if info else 0.0

    # نقاط تلاش — از نقطه دلخواه تا اواسط وای نزدیک ابتدا
    candidates: List[float] = []
    if duration > 0:
        candidates = [
            min(at, max(duration - 0.5, 0.1)),
            max(duration * 0.10, 0.1),
            max(duration * 0.25, 0.1),
            max(duration * 0.50, 0.1),
            0.0,
        ]
    else:
        candidates = [at, 0.0]

    for ts in candidates:
        out = _temp_out("jpg")
        try:
            subprocess.run([
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{ts:.3f}",
                "-i", src,
                "-frames:v", "1",
                "-vf", f"scale={size}:-2:flags=lanczos",
                "-q:v", "2",
                out,
            ], check=True, timeout=_THUMB_TIMEOUT,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out
            if os.path.exists(out):
                os.remove(out)
        except Exception:
            if os.path.exists(out):
                try: os.remove(out)
                except Exception: pass

    return None


def extract_embedded_art(src: str) -> Optional[str]:
    """اگه فایل کاور embed شده داشت، همون رو استخراج می‌کنه."""
    out = _temp_out("jpg")
    try:
        subprocess.run([
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src, "-an", "-vcodec", "copy", "-map", "0:v?",
            "-frames:v", "1", out,
        ], check=True, timeout=_THUMB_TIMEOUT,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
    except Exception:
        pass
    if os.path.exists(out):
        try: os.remove(out)
        except Exception: pass
    return None


# --------------------------------------------------------------------------- #
# Core conversion
# --------------------------------------------------------------------------- #

def _build_cmd(
    src: str,
    out: str,
    preset: Dict[str, Any],
    normalize: bool,
    trim_silence: bool,
    metadata: Optional[Dict[str, str]],
) -> List[str]:
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-vn"]

    # فیلترها
    filters: List[str] = []
    if trim_silence:
        filters.append(
            "silenceremove="
            "start_periods=1:start_silence=0.3:start_threshold=-45dB:"
            "stop_periods=1:stop_silence=0.3:stop_threshold=-45dB"
        )
    if normalize:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

    if filters:
        cmd += ["-af", ",".join(filters)]

    # کدک و بیتریت
    cmd += ["-c:a", preset["codec"]]
    if preset.get("bitrate"):
        cmd += ["-b:a", preset["bitrate"]]
    cmd += preset.get("extra", []) or []

    # متادیتا
    if metadata:
        for k, v in metadata.items():
            if v:
                cmd += ["-metadata", f"{k}={v}"]

    cmd += ["-map_metadata", "-1", "-id3v2_version", "3"]
    cmd += [out]
    return cmd


def convert(
    src: str,
    preset_key: str = "mp3_192",
    *,
    normalize: bool = False,
    trim_silence: bool = False,
    metadata: Optional[Dict[str, str]] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> ConvertResult:
    """
    تبدیل ویدیو/صدا به فرمت دلخواه با preset.

    preset_key یکی از PRESETS:
      mp3_128 | mp3_192 | mp3_320 | mp3_v0
      m4a_128 | m4a_192 | m4a_256
      voice | flac | wav
    """
    assert_ffmpeg()

    if preset_key not in PRESETS:
        raise ConvertError(f"preset نامعتبر: {preset_key}")

    if not os.path.exists(src):
        raise InputError(f"فایل پیدا نشد: {src}")

    if not is_valid_media(src):
        raise InputError("فایل ورودی رسانه معتبری نیست")

    preset = PRESETS[preset_key]
    ext = _EXT_MAP.get(preset["fmt"], "mp3")
    out = _temp_out(ext)

    cmd = _build_cmd(src, out, preset, normalize, trim_silence, metadata)

    start = time.time()
    try:
        proc = subprocess.run(
            cmd, check=False, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        _safe_rm(out)
        raise TimeoutError_(f"تبدیل بعد از {timeout}s timeout خورد")

    elapsed = time.time() - start

    if proc.returncode != 0 or not os.path.exists(out):
        err = (proc.stderr or b"").decode("utf-8", errors="ignore")[-500:]
        _safe_rm(out)
        raise FFmpegFailed(f"FFmpeg با کد {proc.returncode} برگشت", err)

    size = os.path.getsize(out)
    if size == 0:
        _safe_rm(out)
        raise FFmpegFailed("فایل خروجی خالیه")

    return ConvertResult(
        path=out,
        fmt=preset["fmt"],
        bitrate=preset.get("bitrate") or "VBR",
        size=size,
        duration=probe_duration(out),
        elapsed=elapsed,
    )


# --------------------------------------------------------------------------- #
# Backwards-compatible API (برای main.py فعلی)
# --------------------------------------------------------------------------- #

def convert_to_audio(src: str, fmt: str = "mp3", bitrate: str = "192k") -> str:
    """
    سازگاری با API قدیمی.
    fmt: mp3 | m4a | voice
    bitrate: 128k | 192k | 320k | 48k
    """
    key = f"{fmt}_{bitrate.replace('k', '')}"
    if key not in PRESETS:
        # نگاشت ساده اگر presets نبود
        key = {"mp3": "mp3_192", "m4a": "m4a_192", "voice": "voice"}.get(fmt, "mp3_192")
    return convert(src, key).path


# --------------------------------------------------------------------------- #
# Cleanup helpers
# --------------------------------------------------------------------------- #

def _safe_rm(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def cleanup(*paths: Optional[str]) -> None:
    """حذف امن چند فایل."""
    for p in paths:
        _safe_rm(p)


def cleanup_dir(prefix: str = "hivo_", max_age: int = 3600) -> int:
    """
    پاک‌سازی فایل‌های temp قدیمی.
    برمی‌گردونه تعداد فایل‌های حذف‌شده.
    """
    tmp = tempfile.gettempdir()
    now = time.time()
    deleted = 0
    try:
        for name in os.listdir(tmp):
            if not name.startswith(prefix):
                continue
            path = os.path.join(tmp, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age:
                    os.remove(path)
                    deleted += 1
            except Exception:
                continue
    except Exception:
        pass
    return deleted


# --------------------------------------------------------------------------- #
# Utility
# --------------------------------------------------------------------------- #

def generate_waveform(src: str, out_size: str = "800x120") -> Optional[str]:
    """تولید تصویر موج صوتی (برای بخش‌های بصری)."""
    out = _temp_out("png")
    try:
        subprocess.run([
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src,
            "-filter_complex",
            f"showwavespic=s={out_size}:colors=#00d4ff|#7b2ff7",
            "-frames:v", "1", out,
        ], check=True, timeout=120,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if os.path.exists(out) else None
    except Exception:
        _safe_rm(out)
        return None


def extract_audio_only(src: str) -> Optional[str]:
    """استخراج استریم صوتی بدون re-encode (سریع)."""
    if not os.path.exists(src):
        return None
    try:
        info = probe(src)
        if not info.audio_codec:
            return None
        ext = {"aac": "m4a", "mp3": "mp3", "opus": "ogg", "vorbis": "ogg"}.get(
            info.audio_codec, "mka"
        )
        out = _temp_out(ext)
        subprocess.run([
            FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-i", src, "-vn", "-c:a", "copy", out,
        ], check=True, timeout=300,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if os.path.exists(out) else None
    except Exception:
        return None
