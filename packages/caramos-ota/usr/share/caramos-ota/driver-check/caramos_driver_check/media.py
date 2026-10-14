"""Pictures and videos for help requests: checked, made small and stripped of private data before upload.

Pictures (Pillow; GdkPixbuf opens what Pillow cannot, such as HEIC photos from phones) are turned the right
way up, shrunk to at most MAX_PIXELS and saved as WebP: a screenshot lossless while that stays small, so its
text stays sharp, a photo (JPEG, HEIC: lossy already) or a big screenshot lossy. EXIF data, with the place a
phone photo was taken, is left behind. Videos (ffmpeg) last at most a minute and become H.264/AAC MP4 of at most 1920 pixels and 30
frames a second at about 2 Mbit/s, also without metadata. Screenshots come from Flameshot, screen
recordings from Cinnamon (Ctrl+Shift+Alt+R).

Each form keeps its prepared files in its own folder (new_folder), removed after the request was sent or
when the app ends.
"""

from __future__ import annotations

import atexit
import io
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

IMAGE = "image"
VIDEO = "video"
MAX_FILES = 5                        # VNLF keeps five files per request
MAX_IMAGE_BYTES = 10 * 1024 * 1024   # VNLF's limits
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_VIDEO_SECONDS = 60
MAX_PIXELS = 2560 * 1600             # a 4K screenshot or a 12 MP photo is shrunk to this
MAX_SIDE = 4096                      # a long screenshot keeps readable text
LOSSLESS_BYTES = 1024 * 1024         # a picture this small without losses stays lossless
WEBP_QUALITY = 82
VIDEO_SIDE = 1920
VIDEO_FPS = 30
THUMBNAIL_SIZE = (240, 150)

# Cinnamon names its screen recordings cinnamon-%Y-%m-%dT%H%M%S%z.webm (org.cinnamon.recorder).
RECORDING = re.compile(r"cinnamon-(\d{4}-\d{2}-\d{2}T\d{6}[+-]\d{4})\.webm")
# Pictures a camera took: lossy already, a lossless copy would only be bigger.
PHOTO_FORMATS = {"JPEG", "MPO"}
# HEIF and AVIF pictures are ISO media files like MP4 videos; their brand tells them apart.
PICTURE_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1", b"avif"}

_lock = threading.Lock()
_folders: set[Path] = set()
_processes: set[subprocess.Popen] = set()
_video_slot = threading.Semaphore(1)   # one video at a time: ffmpeg already uses every core


class MediaError(Exception):
    """Why a file cannot be attached: unsupported, unreadable, too-long, too-big, no-ffmpeg or cancelled.

    duration: how long a video that is too long lasts, 0 when only known to be over a minute.
    """

    def __init__(self, reason: str, duration: float = 0.0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.duration = duration


@dataclass(frozen=True)
class Attachment:
    """A prepared file, ready to upload."""

    kind: str                  # IMAGE or VIDEO
    name: str                  # what the user picked, for the form
    path: Path                 # the prepared file in the form's folder
    mime: str
    size: int
    width: int
    height: int
    duration: float = 0.0      # seconds, videos only
    thumbnail: Path | None = None


@dataclass(frozen=True)
class VideoInfo:
    duration: float            # seconds; 0 when the file does not say (a recording still going)
    width: int
    height: int
    audio: bool


def detect(path: Path) -> str:
    """IMAGE, VIDEO or "" (anything else), from the start of the file and else its name."""

    try:
        with path.open("rb") as stream:
            head = stream.read(32)
    except OSError:
        return ""
    if (head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*"))
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")):
        return IMAGE
    if head[4:8] == b"ftyp":
        return IMAGE if head[8:12] in PICTURE_BRANDS else VIDEO
    if head.startswith(b"\x1a\x45\xdf\xa3") or (head[:4] == b"RIFF" and head[8:12] == b"AVI "):
        return VIDEO   # Matroska/WebM, AVI
    mime, _encoding = mimetypes.guess_type(path.name)
    if mime and mime.startswith("image/"):
        return IMAGE
    if mime and mime.startswith("video/"):
        return VIDEO
    return ""


def clock(seconds: float) -> str:
    """0:42, 1:05: how long a video lasts."""

    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def too_long(duration: float) -> bool:
    """Longer than a minute, as players show it (1:00 still goes)."""

    return math.floor(duration) > MAX_VIDEO_SECONDS


def new_folder() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="caramos-help-"))
    with _lock:
        _folders.add(folder)
    return folder


def remove_folder(folder: Path | None) -> None:
    if folder is None:
        return
    with _lock:
        _folders.discard(folder)
    shutil.rmtree(folder, ignore_errors=True)


@atexit.register
def _clean_up() -> None:
    with _lock:
        processes, folders = list(_processes), list(_folders)
    for process in processes:
        _stop(process)
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# The Vietnamese d with a stroke has no decomposition: NFKD alone would drop it.
D_WITH_STROKE = str.maketrans({0x0111: "d", 0x0110: "D"})


def _slug(text: str, fallback: str) -> str:
    """A file name without accents, spaces or extension (the user's names are often Vietnamese)."""

    text = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", text)
    text = unicodedata.normalize("NFKD", text.translate(D_WITH_STROKE))
    text = re.sub(r"[^A-Za-z0-9_-]+", "-", text.encode("ascii", "ignore").decode()).strip("-_")
    return text[:40] or fallback


def _target(folder: Path, stem: str, suffix: str) -> Path:
    """A new file in the folder; it is created at once, so two files prepared together never share it."""

    with _lock:
        for number in range(1, 1000):
            path = folder / (f"{stem}{suffix}" if number == 1 else f"{stem}-{number}{suffix}")
            try:
                os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                continue
            return path
    raise MediaError("unreadable")


# Pictures ------------------------------------------------------------------------------------


def fitted_size(width: int, height: int) -> tuple[int, int]:
    """The size a picture is shrunk to: at most MAX_PIXELS and MAX_SIDE, never larger than it was."""

    scale = min(1.0, math.sqrt(MAX_PIXELS / (width * height)), MAX_SIDE / max(width, height))
    if scale >= 1.0:
        return width, height
    return max(1, int(width * scale)), max(1, int(height * scale))


def prepare_image(source: Path, folder: Path, name: str = "") -> Attachment:
    from PIL import Image, ImageOps   # here: the scan and the windows do not need Pillow

    image = _open_image(source)
    try:
        stem = _slug(name or source.name, IMAGE)
        if image.format == "GIF" and getattr(image, "is_animated", False):
            # An animation stays one; GIF has no place or camera data to remove.
            size = source.stat().st_size
            if size > MAX_IMAGE_BYTES:
                raise MediaError("too-big")
            target = _target(folder, stem, ".gif")
            shutil.copyfile(source, target)
            return Attachment(IMAGE, name or source.name, target, "image/gif", size, image.width, image.height,
                              thumbnail=_thumbnail(image, target))
        # GdkPixbuf opened it (no format): a HEIC or AVIF photo.
        photo = image.format in PHOTO_FORMATS or image.format is None
        picture = _plain(ImageOps.exif_transpose(image))
        width, height = fitted_size(picture.width, picture.height)
        if (width, height) != picture.size:
            picture = picture.resize((width, height), Image.Resampling.LANCZOS)
        data = _webp(picture, lossless=False)
        if not photo:
            lossless = _webp(picture, lossless=True)
            if len(lossless) <= LOSSLESS_BYTES or len(lossless) < len(data):
                data = lossless
        if len(data) > MAX_IMAGE_BYTES:
            raise MediaError("too-big")
        target = _target(folder, stem, ".webp")
        target.write_bytes(data)
        return Attachment(IMAGE, name or source.name, target, "image/webp", len(data), width, height,
                          thumbnail=_thumbnail(picture, target))
    except OSError as exc:
        raise MediaError("unreadable") from exc
    finally:
        image.close()


def _open_image(source: Path):
    from PIL import Image

    try:
        image = Image.open(source)
        image.load()
        return image
    except Image.DecompressionBombError as exc:
        raise MediaError("too-big") from exc
    except (OSError, ValueError, SyntaxError):
        pass
    # HEIC and AVIF photos, SVG drawings: GdkPixbuf has loaders for them.
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib
    except (ImportError, ValueError) as exc:
        raise MediaError("unreadable") from exc
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(source))
    except GLib.Error as exc:
        raise MediaError("unreadable") from exc
    pixbuf = pixbuf.apply_embedded_orientation() or pixbuf
    mode = "RGBA" if pixbuf.get_has_alpha() else "RGB"
    return Image.frombuffer(mode, (pixbuf.get_width(), pixbuf.get_height()), pixbuf.get_pixels(), "raw", mode,
                            pixbuf.get_rowstride(), 1)


def _plain(picture):
    """RGB, or RGBA when something is see-through; without EXIF, XMP or colour profile."""

    if picture.mode in ("RGBA", "LA", "PA") or (picture.mode == "P" and "transparency" in picture.info):
        picture = picture.convert("RGBA")
        if picture.getextrema()[3][0] == 255:
            picture = picture.convert("RGB")
    elif picture.mode != "RGB":
        picture = picture.convert("RGB")
    else:
        picture = picture.copy()
    picture.info = {}
    return picture


def _webp(picture, *, lossless: bool) -> bytes:
    buffer = io.BytesIO()
    if lossless:
        picture.save(buffer, "WEBP", lossless=True, quality=60, method=4)
    else:
        picture.save(buffer, "WEBP", quality=WEBP_QUALITY, method=4)
    return buffer.getvalue()


def _thumbnail(picture, target: Path) -> Path | None:
    """The middle of the picture at the size of the form's tiles."""

    from PIL import Image, ImageOps

    try:
        small = picture.convert("RGBA") if picture.mode not in ("RGB", "RGBA") else picture
        small = ImageOps.fit(small, THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        path = target.with_name(target.stem + ".thumb.png")
        small.save(path, "PNG")
        return path
    except (OSError, ValueError):
        return None


# Videos --------------------------------------------------------------------------------------


def ffmpeg_available(which: Callable[[str], str | None] = shutil.which) -> bool:
    return bool(which("ffmpeg") and which("ffprobe"))


def probe_video(path: Path, run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> VideoInfo:
    try:
        result = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                     check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError("unreadable") from exc
    try:
        data = json.loads(result.stdout or b"{}")
    except ValueError:
        data = {}
    streams = [stream for stream in data.get("streams", []) if isinstance(stream, dict)] if isinstance(data, dict) else []
    # Cover pictures of audio files are "video" streams too.
    video = next((stream for stream in streams if stream.get("codec_type") == "video"
                  and not (stream.get("disposition") or {}).get("attached_pic")), None)
    if result.returncode != 0 or video is None:
        raise MediaError("unreadable")
    duration = _seconds((data.get("format") or {}).get("duration")) or _seconds(video.get("duration"))
    width, height = _number(video.get("width")), _number(video.get("height"))
    if _rotation(video) in (90, 270):
        width, height = height, width
    return VideoInfo(duration, width, height, any(stream.get("codec_type") == "audio" for stream in streams))


def _seconds(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def _number(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rotation(stream: dict) -> int:
    """How a phone turned the video: the rotate tag or the display matrix."""

    values = [(stream.get("tags") or {}).get("rotate")]
    values += [item.get("rotation") for item in stream.get("side_data_list") or [] if isinstance(item, dict)]
    for value in values:
        try:
            return abs(int(float(value))) % 360
        except (TypeError, ValueError):
            continue
    return 0


def video_command(source: Path, target: Path) -> list[str]:
    box = f"min(iw\\,{VIDEO_SIDE}):min(ih\\,{VIDEO_SIDE})"
    return [
        # Low priority: the user keeps writing in the form meanwhile.
        "nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-t", str(MAX_VIDEO_SECONDS + 1),
        "-vf", f"scale={box}:force_original_aspect_ratio=decrease:force_divisible_by=2", "-fpsmax", str(VIDEO_FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-maxrate", "2M", "-bufsize", "4M",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
        # Phones store where and when a video was taken.
        "-map_metadata", "-1", "-map_metadata:s:v", "-1", "-map_metadata:s:a", "-1", "-map_chapters", "-1",
        "-sn", "-dn", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(target),
    ]


def prepare_video(source: Path, folder: Path, name: str = "", on_progress: Callable[[float], None] | None = None,
                  cancel: threading.Event | None = None) -> Attachment:
    if not ffmpeg_available():
        raise MediaError("no-ffmpeg")
    info = probe_video(source)
    if too_long(info.duration):
        raise MediaError("too-long", info.duration)
    target = _target(folder, _slug(name or source.name, VIDEO), ".mp4")
    try:
        with _video_slot:
            if cancel is not None and cancel.is_set():
                raise MediaError("cancelled")
            _run_ffmpeg(video_command(source, target), info.duration or MAX_VIDEO_SECONDS, on_progress, cancel)
        result = probe_video(target)
        if not result.duration:
            raise MediaError("unreadable")
        if too_long(result.duration):
            # The source did not say how long it was; ffmpeg stopped just after a minute. 0: "over a minute".
            raise MediaError("too-long", 0.0 if not info.duration else result.duration)
        size = target.stat().st_size
        if size > MAX_VIDEO_BYTES:
            raise MediaError("too-big")
    except (MediaError, OSError) as exc:
        target.unlink(missing_ok=True)
        if isinstance(exc, MediaError):
            raise
        raise MediaError("unreadable") from exc
    return Attachment(VIDEO, name or source.name, target, "video/mp4", size, result.width, result.height,
                      result.duration, _video_thumbnail(target, result.duration))


def _run_ffmpeg(command: list[str], expected: float, on_progress: Callable[[float], None] | None,
                cancel: threading.Event | None) -> None:
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
    except OSError as exc:
        raise MediaError("no-ffmpeg") from exc
    with _lock:
        _processes.add(process)
    try:
        # ffmpeg writes its progress twice a second.
        for line in process.stdout:
            if cancel is not None and cancel.is_set():
                _stop(process)
                raise MediaError("cancelled")
            key, _sep, value = line.strip().partition("=")
            if key == "out_time_us" and on_progress is not None:
                on_progress(min(1.0, _seconds(value) / 1_000_000 / expected))
        process.wait()
    finally:
        with _lock:
            _processes.discard(process)
        process.stdout.close()
    if cancel is not None and cancel.is_set():
        raise MediaError("cancelled")
    if process.returncode != 0:
        raise MediaError("unreadable")


def _video_thumbnail(video: Path, duration: float) -> Path | None:
    path = video.with_name(video.stem + ".thumb.png")
    width, height = THUMBNAIL_SIZE
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{min(1.0, duration / 3):.2f}",
               "-i", str(video), "-frames:v", "1", "-vf",
               f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}", str(path)]
    try:
        subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return path if path.is_file() and path.stat().st_size else None


def prepare(source: Path, folder: Path, name: str = "", on_progress: Callable[[float], None] | None = None,
            cancel: threading.Event | None = None) -> Attachment:
    kind = detect(source)
    if kind == IMAGE:
        return prepare_image(source, folder, name)
    if kind == VIDEO:
        return prepare_video(source, folder, name, on_progress, cancel)
    raise MediaError("unsupported")


# Screenshots and screen recordings -----------------------------------------------------------


def screenshot_tool(which: Callable[[str], str | None] = shutil.which) -> str:
    for tool in ("flameshot", "gnome-screenshot"):
        if which(tool):
            return tool
    return ""


def take_screenshot(target: Path, tool: str | None = None,
                    run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    """The user picks part of the screen; blocks until it was taken (True) or cancelled (False)."""

    tool = screenshot_tool() if tool is None else tool
    try:
        if tool == "flameshot":
            # Like the session's Flameshot, which CaramOS starts without OpenGL.
            env = dict(os.environ, QT_XCB_GL_INTEGRATION="none")
            result = run(["flameshot", "gui", "--raw"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=env, timeout=900, check=False)
            # Esc: nothing on stdout.
            if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
                return False
            target.write_bytes(result.stdout)
            return True
        if tool == "gnome-screenshot":
            run(["gnome-screenshot", "--area", "--file", str(target)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900, check=False)
            return target.is_file() and target.stat().st_size > 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False


def recordings_folder(videos: str | None, home: Path | None = None) -> Path:
    """Where Cinnamon saves screen recordings: the Videos folder, or the home folder without one."""

    if videos and Path(videos).is_dir():
        return Path(videos)
    return home or Path.home()


def recording_started(name: str) -> datetime | None:
    """When a screen recording of Cinnamon began, from its file name; None for other files."""

    match = RECORDING.fullmatch(name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%dT%H%M%S%z")
    except ValueError:
        return None


def recording_finished(path: Path) -> float:
    """The recording's length once Cinnamon stopped it, else 0: the length is written at the end."""

    try:
        return probe_video(path).duration
    except MediaError:
        return 0.0
