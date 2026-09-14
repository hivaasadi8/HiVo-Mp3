import os, subprocess, tempfile, shutil, uuid

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def probe_duration(path):
    try:
        out = subprocess.check_output([
            FFPROBE, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path
        ], timeout=30).decode().strip()
        return float(out)
    except Exception:
        return 0.0


def convert_to_audio(src, fmt="mp3", bitrate="192k"):
    """fmt: mp3 | m4a | voice"""
    fmt = fmt.lower()
    ext = {"mp3": "mp3", "m4a": "m4a", "voice": "ogg"}.get(fmt, "mp3")
    out = os.path.join(tempfile.gettempdir(), f"hivo_{uuid.uuid4().hex}.{ext}")

    cmd = [FFMPEG, "-y", "-i", src, "-vn"]
    if fmt == "voice":
        cmd += ["-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1"]
    elif fmt == "m4a":
        cmd += ["-c:a", "aac", "-b:a", bitrate]
    else:
        cmd += ["-c:a", "libmp3lame", "-b:a", bitrate]
    cmd += [out]

    subprocess.run(cmd, check=True, timeout=1800,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def extract_thumbnail(src):
    """یه فریم از ثانیه ۱ ویدیو به عنوان کاور."""
    try:
        out = os.path.join(tempfile.gettempdir(), f"thumb_{uuid.uuid4().hex}.jpg")
        subprocess.run([
            FFMPEG, "-y", "-i", src, "-ss", "00:00:01.000", "-vframes", "1",
            "-vf", "scale=320:-1", out
        ], check=True, timeout=60,
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out if os.path.exists(out) else None
    except Exception:
        return None
