"""Pictures and videos of help requests: what is sent is small, upright and without private metadata."""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# Driver Check lives in /usr/share/caramos-ota/driver-check, outside dist-packages.
sys.path.insert(0, str(Path(__file__).parents[1] / "usr/share/caramos-ota/driver-check"))

from caramos_driver_check import media  # noqa: E402

try:
    from PIL import Image, ImageDraw
except ImportError:   # Pillow comes with Cinnamon; a bare build machine may lack it
    Image = None

FFMPEG = media.ffmpeg_available()


def screenshot_like(width: int = 1280, height: int = 800):
    """Flat colours and lines of text, like a window."""

    picture = Image.new("RGB", (width, height), (247, 243, 233))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 0, width, 48), fill=(31, 79, 50))
    for row in range(60, height - 20, 22):
        draw.text((24, row), "Wi-Fi không thấy mạng nào sau khi cài CaramOS %d" % row, fill=(31, 42, 34))
    return picture


def photo_like(width: int, height: int):
    """Noise: nothing a lossless picture can make small."""

    generator = random.Random(4)
    return Image.frombytes("RGB", (width, height), bytes(generator.getrandbits(8) for _ in range(width * height * 3)))


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "prepared"
        self.folder.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()


class DetectTests(Folder):
    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_kinds_from_the_start_of_the_file(self) -> None:
        self.assertEqual(media.IMAGE, media.detect(self.write("a", b"\x89PNG\r\n\x1a\n" + b"0" * 24)))
        self.assertEqual(media.IMAGE, media.detect(self.write("b", b"\xff\xd8\xff\xe0" + b"0" * 24)))
        self.assertEqual(media.IMAGE, media.detect(self.write("c", b"GIF89a" + b"0" * 24)))
        self.assertEqual(media.IMAGE, media.detect(self.write("d", b"RIFF\x00\x00\x00\x00WEBPVP8 ")))
        # HEIC photos and MP4 videos are both ISO media files: the brand decides.
        self.assertEqual(media.IMAGE, media.detect(self.write("e", b"\x00\x00\x00\x18ftypheic" + b"0" * 16)))
        self.assertEqual(media.VIDEO, media.detect(self.write("f", b"\x00\x00\x00\x18ftypisom" + b"0" * 16)))
        self.assertEqual(media.VIDEO, media.detect(self.write("g", b"\x1a\x45\xdf\xa3" + b"0" * 24)))

    def test_else_from_the_name(self) -> None:
        self.assertEqual(media.VIDEO, media.detect(self.write("clip.mov", b"????")))
        self.assertEqual(media.IMAGE, media.detect(self.write("drawing.svg", b"<svg/>")))
        self.assertEqual("", media.detect(self.write("notes.txt", b"hello")))
        self.assertEqual("", media.detect(self.root / "missing.png"))

    def test_prepare_refuses_other_files(self) -> None:
        with self.assertRaises(media.MediaError) as caught:
            media.prepare(self.write("notes.txt", b"hello"), self.folder)
        self.assertEqual("unsupported", caught.exception.reason)


class NameTests(unittest.TestCase):
    def test_names_without_accents_or_spaces(self) -> None:
        self.assertEqual("Anh-loi-Wi-Fi-day", media._slug("Ảnh lỗi Wi-Fi đây", "image"))
        self.assertEqual("image", media._slug("😀 ", "image"))
        self.assertEqual("02-indigo-night-2k", media._slug("02-indigo-night-2k.jpg", "image"))
        self.assertEqual("Loa-re-khi-mo-nhac", media._slug("Loa rè khi mở nhạc.mp4", "video"))

    def test_lengths(self) -> None:
        self.assertEqual("0:42", media.clock(42.7))
        self.assertEqual("1:05", media.clock(65))
        # "Up to a minute" as players show it.
        self.assertFalse(media.too_long(60.9))
        self.assertTrue(media.too_long(61.0))

    def test_fitted_size(self) -> None:
        self.assertEqual((1920, 1080), media.fitted_size(1920, 1080))
        width, height = media.fitted_size(3840, 2160)
        self.assertLessEqual(width * height, media.MAX_PIXELS)
        self.assertAlmostEqual(3840 / 2160, width / height, places=2)
        width, height = media.fitted_size(1080, 9000)   # a long screenshot
        self.assertLessEqual(height, media.MAX_SIDE)
        self.assertGreater(width, 400)


@unittest.skipIf(Image is None, "Pillow is not installed")
class ImageTests(Folder):
    def save(self, picture, name: str, **options) -> Path:
        path = self.root / name
        picture.save(path, **options)
        return path

    def test_a_screenshot_stays_lossless(self) -> None:
        source = screenshot_like()
        attachment = media.prepare(self.save(source, "shot.png"), self.folder, "Ảnh chụp màn hình")
        self.assertEqual((media.IMAGE, "image/webp", 1280, 800), (attachment.kind, attachment.mime,
                                                                   attachment.width, attachment.height))
        self.assertEqual("Anh-chup-man-hinh.webp", attachment.path.name)
        self.assertEqual(attachment.size, attachment.path.stat().st_size)
        self.assertLessEqual(attachment.size, media.LOSSLESS_BYTES)
        with Image.open(attachment.path) as sent:
            self.assertEqual(source.tobytes(), sent.convert("RGB").tobytes())   # the text stays sharp

    def test_a_big_screenshot_becomes_lossy(self) -> None:
        source = photo_like(900, 700)
        path = self.save(source, "noise.png")
        attachment = media.prepare(path, self.folder)
        self.assertEqual("image/webp", attachment.mime)
        self.assertLess(attachment.size, path.stat().st_size / 2)

    def test_a_photo_stays_lossy_and_gets_smaller(self) -> None:
        # A lossless copy of a JPEG would only be bigger than the JPEG.
        path = self.save(screenshot_like(), "camera.jpg", quality=92)
        attachment = media.prepare(path, self.folder)
        self.assertEqual("image/webp", attachment.mime)
        self.assertLess(attachment.size, path.stat().st_size)

    def test_big_pictures_are_shrunk(self) -> None:
        attachment = media.prepare(self.save(screenshot_like(3840, 2160), "4k.png"), self.folder)
        self.assertLessEqual(attachment.width * attachment.height, media.MAX_PIXELS)
        self.assertAlmostEqual(3840 / 2160, attachment.width / attachment.height, places=2)

    def test_place_and_camera_data_are_removed_and_the_photo_is_upright(self) -> None:
        exif = Image.Exif()
        exif[0x0112] = 6                       # taken with the phone turned: shown rotated by 90°
        exif[0x010F] = "PhoneMaker"
        exif.get_ifd(0x8825)[2] = (10.0, 45.0, 36.0)   # GPS latitude
        path = self.save(photo_like(400, 300), "phone.jpg", exif=exif.tobytes(), quality=90)
        attachment = media.prepare(path, self.folder)
        self.assertEqual((300, 400), (attachment.width, attachment.height))
        with Image.open(attachment.path) as sent:
            self.assertEqual(0, len(sent.getexif()))
            self.assertNotIn("exif", sent.info)
            self.assertNotIn("icc_profile", sent.info)

    def test_an_animation_stays_one(self) -> None:
        frames = [Image.new("RGB", (60, 40), color) for color in ((179, 65, 42), (47, 112, 72))]
        path = self.root / "moving.gif"
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=200, loop=0)
        attachment = media.prepare(path, self.folder)
        self.assertEqual("image/gif", attachment.mime)
        self.assertEqual(path.read_bytes(), attachment.path.read_bytes())

    def test_transparency_is_kept_only_when_used(self) -> None:
        opaque = media.prepare(self.save(Image.new("RGBA", (50, 50), (10, 20, 30, 255)), "opaque.png"), self.folder)
        with Image.open(opaque.path) as sent:
            self.assertEqual("RGB", sent.mode)
        clear = media.prepare(self.save(Image.new("RGBA", (50, 50), (10, 20, 30, 0)), "clear.png"), self.folder)
        with Image.open(clear.path) as sent:
            self.assertEqual("RGBA", sent.mode)

    def test_thumbnail_and_names_of_two_files(self) -> None:
        path = self.save(screenshot_like(), "same.png")
        first = media.prepare(path, self.folder, "Screenshot")
        second = media.prepare(path, self.folder, "Screenshot")
        self.assertNotEqual(first.path, second.path)
        with Image.open(first.thumbnail) as thumbnail:
            self.assertEqual(media.THUMBNAIL_SIZE, thumbnail.size)

    def test_a_broken_picture(self) -> None:
        path = self.root / "broken.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        with self.assertRaises(media.MediaError) as caught:
            media.prepare(path, self.folder)
        self.assertEqual("unreadable", caught.exception.reason)


class ProbeTests(unittest.TestCase):
    def probe(self, answer: dict, returncode: int = 0) -> media.VideoInfo:
        result = subprocess.CompletedProcess([], returncode, json.dumps(answer).encode(), b"")
        return media.probe_video(Path("clip.mp4"), run=lambda *_args, **_kwargs: result)

    def test_length_size_and_sound(self) -> None:
        info = self.probe({"format": {"duration": "42.5"}, "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080}, {"codec_type": "audio"}]})
        self.assertEqual(media.VideoInfo(42.5, 1920, 1080, True), info)

    def test_a_phone_video_turned_on_its_side(self) -> None:
        info = self.probe({"format": {"duration": "3"}, "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]}]})
        self.assertEqual((1080, 1920, False), (info.width, info.height, info.audio))

    def test_a_recording_still_going_has_no_length(self) -> None:
        info = self.probe({"format": {"duration": "N/A"}, "streams": [{"codec_type": "video", "width": 1280,
                                                                       "height": 800}]})
        self.assertEqual(0.0, info.duration)

    def test_no_picture_is_no_video(self) -> None:
        for answer, code in (({"streams": [{"codec_type": "audio"},
                                           {"codec_type": "video", "disposition": {"attached_pic": 1}}]}, 0),
                             ({}, 1)):
            with self.assertRaises(media.MediaError):
                self.probe(answer, code)

    def test_the_ffmpeg_command(self) -> None:
        command = media.video_command(Path("in.webm"), Path("out.mp4"))
        self.assertEqual(["nice", "-n", "10", "ffmpeg"], command[:4])
        joined = " ".join(command)
        for part in ("-c:v libx264", "-t 61", "-map_metadata -1", "-map 0:a:0?", "-movflags +faststart",
                     "min(iw\\,1920):min(ih\\,1920)", "-fpsmax 30", "-progress pipe:1"):
            self.assertIn(part, joined)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class VideoTests(Folder):
    def make(self, name: str, seconds: float, size: str = "320x240", **metadata: str) -> Path:
        path = self.root / name
        tags = [option for key, value in metadata.items() for option in ("-metadata", f"{key}={value}")]
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc2=size={size}:rate=15", "-f", "lavfi", "-i", "sine=frequency=440", "-t", str(seconds),
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", *tags, str(path)], check=True)
        return path

    def tags(self, path: Path) -> dict:
        result = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
                                capture_output=True, check=True)
        return json.loads(result.stdout)["format"].get("tags", {})

    def test_a_video_becomes_a_small_mp4_without_metadata(self) -> None:
        source = self.make("clip.mp4", 3, location="+10.7626+106.6602/", title="Nhà riêng")
        self.assertIn("location", self.tags(source))
        steps: list[float] = []
        attachment = media.prepare(source, self.folder, "Quay màn hình", on_progress=steps.append)
        self.assertEqual((media.VIDEO, "video/mp4", 320, 240), (attachment.kind, attachment.mime,
                                                                 attachment.width, attachment.height))
        self.assertAlmostEqual(3.0, attachment.duration, delta=0.2)
        self.assertTrue(set(self.tags(attachment.path)) <= {"major_brand", "minor_version", "compatible_brands",
                                                            "encoder"})
        self.assertTrue(attachment.thumbnail and attachment.thumbnail.is_file())
        self.assertTrue(steps and all(0 <= step <= 1 for step in steps))

    def test_a_big_video_is_made_smaller(self) -> None:
        attachment = media.prepare(self.make("big.mp4", 1, "2560x1440"), self.folder)
        self.assertEqual((1920, 1080), (attachment.width, attachment.height))

    def test_videos_over_a_minute_are_refused(self) -> None:
        with self.assertRaises(media.MediaError) as caught:
            media.prepare(self.make("long.mp4", 62, "64x48"), self.folder)
        self.assertEqual("too-long", caught.exception.reason)
        self.assertAlmostEqual(62, caught.exception.duration, delta=0.5)
        self.assertEqual([], list(self.folder.iterdir()))

    def test_cancelled(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(media.MediaError) as caught:
            media.prepare(self.make("clip.mp4", 2), self.folder, cancel=cancel)
        self.assertEqual("cancelled", caught.exception.reason)
        self.assertEqual([], list(self.folder.iterdir()))


class ScreenshotTests(Folder):
    def test_flameshot(self) -> None:
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs["env"].get("QT_XCB_GL_INTEGRATION")))
            return subprocess.CompletedProcess(command, 0, b"\x89PNG\r\n\x1a\nPICTURE", b"")

        target = self.root / "shot.png"
        self.assertTrue(media.take_screenshot(target, "flameshot", run=run))
        self.assertEqual(b"\x89PNG\r\n\x1a\nPICTURE", target.read_bytes())
        # Like the session's Flameshot, without OpenGL.
        self.assertEqual([(["flameshot", "gui", "--raw"], "none")], calls)

    def test_esc_in_flameshot(self) -> None:
        target = self.root / "shot.png"
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
        self.assertFalse(media.take_screenshot(target, "flameshot", run=run))
        self.assertFalse(target.exists())

    def test_gnome_screenshot_without_flameshot(self) -> None:
        target = self.root / "shot.png"

        def run(command, **_kwargs):
            Path(command[-1]).write_bytes(b"\x89PNG")
            return subprocess.CompletedProcess(command, 0)

        self.assertTrue(media.take_screenshot(target, "gnome-screenshot", run=run))
        self.assertFalse(media.take_screenshot(self.root / "none.png", ""))
        self.assertEqual("gnome-screenshot", media.screenshot_tool(lambda name: name == "gnome-screenshot" or None))
        self.assertEqual("flameshot", media.screenshot_tool(lambda name: name))


class RecordingTests(Folder):
    def test_cinnamon_recordings_and_when_they_began(self) -> None:
        started = media.recording_started("cinnamon-2026-10-13T092548+0700.webm")
        self.assertEqual(datetime(2026, 10, 13, 9, 25, 48, tzinfo=timezone(timedelta(hours=7))), started)
        for name in ("cinnamon-2026-10-13T092548+0700.mp4", "holiday.webm", "cinnamon-today.webm"):
            self.assertIsNone(media.recording_started(name))

    def test_the_folder_cinnamon_records_to(self) -> None:
        self.assertEqual(self.folder, media.recordings_folder(str(self.folder), self.root))
        self.assertEqual(self.root, media.recordings_folder(str(self.root / "Videos"), self.root))
        self.assertEqual(self.root, media.recordings_folder(None, self.root))

    def test_a_recording_is_finished_once_it_has_a_length(self) -> None:
        with mock.patch.object(media, "probe_video", return_value=media.VideoInfo(0.0, 1280, 800, False)):
            self.assertEqual(0.0, media.recording_finished(Path("going.webm")))
        with mock.patch.object(media, "probe_video", return_value=media.VideoInfo(12.5, 1280, 800, False)):
            self.assertEqual(12.5, media.recording_finished(Path("done.webm")))
        with mock.patch.object(media, "probe_video", side_effect=media.MediaError("unreadable")):
            self.assertEqual(0.0, media.recording_finished(Path("broken.webm")))


class CleanupTests(unittest.TestCase):
    def test_folders_and_running_ffmpeg_go_when_the_app_ends(self) -> None:
        folder = media.new_folder()
        (folder / "a.webp").write_bytes(b"x")
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(media, "_processes", {process}):
            media._clean_up()
        self.assertFalse(folder.exists())
        process.terminate.assert_called_once_with()
        media.remove_folder(folder)   # again: nothing left to do
        media.remove_folder(None)

    def test_a_removed_folder_is_forgotten(self) -> None:
        folder = media.new_folder()
        media.remove_folder(folder)
        self.assertFalse(folder.exists())
        self.assertNotIn(folder, media._folders)


if __name__ == "__main__":
    unittest.main()
