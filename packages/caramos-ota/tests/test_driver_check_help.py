"""Requests to VNLF: what is sent to the help API, how every answer is handled and what is kept."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

# Driver Check lives in /usr/share/caramos-ota/driver-check, outside dist-packages.
sys.path.insert(0, str(Path(__file__).parents[1] / "usr/share/caramos-ota/driver-check"))

from caramos_driver_check import helpdesk  # noqa: E402

ROOT = Path(__file__).parents[1]
REPORT = {
    "findings": [
        {"key": "wifi:broadcom:pci:14e4:43a0", "level": "critical", "title": "Wi-Fi needs the Broadcom driver",
         "device": "Broadcom BCM4360", "device_id": "PCI 14e4:43a0"},
        {"key": "fingerprint:supported:usb:04f3:0c7e", "level": "tip", "title": "Log in with your fingerprint",
         "device": "", "device_id": ""},
    ],
}


class Server:
    """A local HTTP server answering every POST with a given status and JSON body."""

    def __init__(self, status: int, answer: dict | None = None, headers: dict | None = None) -> None:
        self.requests: list[tuple[dict, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):   # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append((json.loads(self.rfile.read(length)), dict(self.headers)))
                body = json.dumps(answer or {}).encode()
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/caramos/help/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class UploadServer:
    """The attachment steps of the API (an upload link, complete) and the storage the link points to."""

    def __init__(self, presign: int = 201, put: int = 200, complete: int = 200, answer: dict | None = None,
                 headers: dict | None = None) -> None:
        self.calls: list[tuple[str, str, dict, bytes]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):   # noqa: N802 - http.server API
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.calls.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, data))
                if self.path.endswith("/complete"):
                    self.reply(complete, {"id": "a1", "status": "READY"} if complete == 200 else {"error": "invalid"})
                elif presign == 201:
                    mime = json.loads(data)["mime"]
                    self.reply(201, answer or {"id": "a1", "upload_url": f"{outer.base}/storage/a1?sig=x",
                                               "headers": {"content-type": mime}, "expires_in": 600})
                else:
                    self.reply(presign, {"error": "rate_limited", "retry_after": 3600} if presign == 429 else {})

            def do_PUT(self):   # noqa: N802 - http.server API
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.calls.append(("PUT", self.path, {k.lower(): v for k, v in self.headers.items()}, data))
                self.send_response(put)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.url = f"{self.base}/api/caramos/help/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class StatusServer:
    """The status of requests: a status and JSON body for each token, VNLF's 404 for the others."""

    def __init__(self, answers: dict[str, tuple[int, dict]]) -> None:
        self.calls: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):   # noqa: N802 - http.server API
                outer.calls.append((self.path, dict(self.headers)))
                status, body = answers.get(self.path.rsplit("/", 1)[-1],
                                           (404, {"error": "not_found", "message": "Không tìm thấy yêu cầu hỗ trợ này."}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/caramos/help/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


TOKEN = "CczzLA-qizFryItIR1CDox5cgYtARl5j7aahhiVd-00"
PAGE = f"https://vietnamlinuxfamily.net/caramos/help/{TOKEN}"


def status_answer(status: str = "OPEN", reply: str | None = None, consent: bool | None = True, url: str | None = None,
                  resolved: str | None = None, answers: int = 0) -> dict:
    """GET /requests/<token> as VNLF answers it (apps/api/src/modules/caramos-help.ts)."""

    return {"id": "H-1001", "kind": "OS_SUPPORT", "status": status, "message": "Không có tiếng sau khi cập nhật",
            "reply": reply, "caramos": None, "createdAt": "2026-10-14T08:29:11.204Z",
            "updatedAt": "2026-10-15T02:00:00.000Z", "resolvedAt": resolved,
            "attachments": {"images": 1, "videos": 0}, "public": {"consent": consent, "url": url},
            "answers": [{"id": f"01a1-{number}", "content": "Thử cắm lại tai nghe rồi chọn Tai nghe trong Âm thanh.",
                         "votes": 3 - number, "accepted": number == 0, "author": "Dũng Lê",
                         "createdAt": "2026-10-14T09:01:11.000Z"} for number in range(answers)]}


class RequestTests(unittest.TestCase):
    def test_message_length(self) -> None:
        self.assertEqual("short", helpdesk.message_problem("  Wi-Fi  "))
        self.assertEqual("", helpdesk.message_problem("Wi-Fi does not work after installing"))
        self.assertEqual("long", helpdesk.message_problem("x" * (helpdesk.MAX_MESSAGE + 1)))

    def test_request_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / "caramos-release"
            release.write_text('NAME="CaramOS"\nVERSION="1.0.22"\n', encoding="utf-8")
            with mock.patch.object(helpdesk, "RELEASE_FILE", release):
                payload = helpdesk.build_request("  Wi-Fi does not work  ", " zalo 0901 ", "https://linux-hardware.org/?probe=ab12",
                                                 REPORT, kernel="6.14.0-37-generic", live=True)
        self.assertEqual(1, payload["schema"])
        self.assertEqual("Wi-Fi does not work", payload["message"])
        self.assertEqual("zalo 0901", payload["contact"])
        self.assertEqual("https://linux-hardware.org/?probe=ab12", payload["probe_url"])
        self.assertEqual(REPORT, payload["report"])
        self.assertEqual({"caramos": "1.0.22", "kernel": "6.14.0-37-generic", "live": True, "virtual": False},
                         {key: payload["system"][key] for key in ("caramos", "kernel", "live", "virtual")})
        self.assertEqual("driver", payload["type"])
        self.assertEqual({"app": "caramos-driver-check", "version": "1.0.22"}, payload["client"])

    def test_kind_and_app_of_feedback_and_help(self) -> None:
        payload = helpdesk.build_request("The Update Center stops halfway", kind="bug", app="caramos-help")
        self.assertEqual(("bug", "caramos-help"), (payload["type"], payload["client"]["app"]))
        self.assertEqual(("driver", "os", "bug", "feedback"), helpdesk.KINDS)

    def test_lengths_are_counted_like_the_api(self) -> None:
        # The API counts JavaScript string length: UTF-16 code units, an emoji is 2.
        self.assertEqual(2, helpdesk.length("😀"))
        self.assertEqual("short", helpdesk.message_problem("😀" * 4))
        self.assertEqual("", helpdesk.message_problem("😀" * 5))
        self.assertEqual("long", helpdesk.message_problem("😀" * 2501))
        self.assertEqual("😀" * 100, helpdesk.build_request("Wi-Fi does not work", "😀" * 150)["contact"])
        # Cut before a character that does not fit, never in the middle of it.
        self.assertEqual("a" + "😀" * 99, helpdesk.build_request("Wi-Fi does not work", "a" + "😀" * 150)["contact"])

    def test_a_report_over_the_api_limit_is_left_out(self) -> None:
        big = {"findings": [{"detail": "x" * 1000}] * 70}
        self.assertNotIn("report", helpdesk.build_request("Wi-Fi does not work", report=big))
        self.assertEqual(REPORT, helpdesk.build_request("Wi-Fi does not work", report=REPORT)["report"])

    def test_optional_parts_are_left_out(self) -> None:
        payload = helpdesk.build_request("Bluetooth does not find my headset")
        self.assertNotIn("probe_url", payload)
        self.assertNotIn("report", payload)
        self.assertEqual("", payload["contact"])

    def test_every_request_says_whether_the_community_may_see_it(self) -> None:
        self.assertIs(True, helpdesk.build_request("Bluetooth does not find my headset", public=True)["public"])
        # Unticked: false, not left out (VNLF reads a missing one as a version that never asked).
        self.assertIs(False, helpdesk.build_request("Bluetooth does not find my headset")["public"])

    def test_text_to_paste_by_hand(self) -> None:
        payload = helpdesk.build_request("Wi-Fi does not work", "me@example.com", "https://linux-hardware.org/?probe=ab12",
                                         REPORT, kernel="6.14.0-37-generic")
        text = helpdesk.as_text(payload)
        self.assertTrue(text.startswith("Wi-Fi does not work\n"))
        self.assertIn("https://linux-hardware.org/?probe=ab12", text)
        self.assertIn("- Wi-Fi needs the Broadcom driver (Broadcom BCM4360 PCI 14e4:43a0)", text)
        self.assertNotIn("fingerprint", text)   # tips are not problems
        self.assertTrue(text.rstrip().endswith("me@example.com"))

    def test_the_request_names_its_pictures_and_videos(self) -> None:
        payload = helpdesk.build_request("The panel froze after an update", kind="bug", app="caramos-help",
                                         upload_id="1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", attachments=("a1", "a2"))
        self.assertEqual("1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", payload["upload_id"])
        self.assertEqual(["a1", "a2"], payload["attachments"])
        without = helpdesk.build_request("The panel froze after an update", upload_id="x")
        self.assertNotIn("upload_id", without)
        self.assertNotIn("attachments", without)
        text = helpdesk.as_text(without, ["Anh-chup-man-hinh.webp (58.3 kB)", "Quay-man-hinh.mp4 (3.1 MB)"])
        self.assertIn("Pictures and videos: Anh-chup-man-hinh.webp (58.3 kB), Quay-man-hinh.mp4 (3.1 MB)", text)

    def test_the_address_can_be_overridden_for_testing(self) -> None:
        with mock.patch.dict("os.environ", {"CARAMOS_DRIVER_CHECK_HELP_URL": "http://127.0.0.1:9/x"}):
            self.assertEqual("http://127.0.0.1:9/x", helpdesk.help_url())
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(helpdesk.HELP_URL, helpdesk.help_url())
        self.assertTrue(helpdesk.HELP_URL.startswith("https://"))


class SendTests(unittest.TestCase):
    def send(self, status: int, answer: dict | None = None, headers: dict | None = None):
        server = Server(status, answer, headers)
        try:
            payload = helpdesk.build_request("Wi-Fi does not work after installing", report=REPORT)
            result = helpdesk.send(payload, server.url, timeout=5)
        finally:
            server.close()
        return result, server.requests

    def test_accepted(self) -> None:
        result, requests = self.send(201, {"id": "H-1042", "url": "https://vietnamlinuxfamily.net/p/1042"})
        self.assertEqual((True, "", "H-1042", "https://vietnamlinuxfamily.net/p/1042"),
                         (result.ok, result.error, result.request_id, result.url))
        body, headers = requests[0]
        self.assertEqual("Wi-Fi does not work after installing", body["message"])
        self.assertEqual(REPORT, body["report"])
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertTrue(headers["User-Agent"].startswith("caramos-driver-check/"))

    def test_sharing_goes_with_the_request(self) -> None:
        server = Server(201, {"id": "H-1", "url": "https://vietnamlinuxfamily.net/caramos/help/a"})
        try:
            payload = helpdesk.build_request("Wi-Fi does not work after installing", public=True)
            self.assertTrue(helpdesk.send(payload, server.url, timeout=5).ok)
        finally:
            server.close()
        self.assertIs(True, server.requests[0][0]["public"])

    def test_the_user_agent_names_the_app(self) -> None:
        server = Server(201, {"id": "H-1", "url": "https://vietnamlinuxfamily.net/caramos/help/a"})
        try:
            payload = helpdesk.build_request("Some feedback about CaramOS", kind="feedback", app="caramos-help")
            self.assertTrue(helpdesk.send(payload, server.url, timeout=5).ok)
        finally:
            server.close()
        body, headers = server.requests[0]
        self.assertEqual("feedback", body["type"])
        self.assertTrue(headers["User-Agent"].startswith("caramos-help/"))

    def test_only_https_links_are_shown(self) -> None:
        result, _requests = self.send(200, {"id": "7", "url": "javascript:alert(1)"})
        self.assertTrue(result.ok)
        self.assertEqual("", result.url)

    def test_errors(self) -> None:
        self.assertEqual("rejected", self.send(400, {"error": "invalid"})[0].error)
        self.assertEqual("unavailable", self.send(404)[0].error)
        self.assertEqual("server", self.send(503, {"error": "unavailable"})[0].error)
        limited = self.send(429, {"error": "rate_limited", "retry_after": 600})[0]
        self.assertEqual(("rate-limited", 600), (limited.error, limited.retry_after))
        header_limited = self.send(429, {}, {"Retry-After": "120"})[0]
        self.assertEqual(120, header_limited.retry_after)

    def test_no_connection(self) -> None:
        server = Server(201)
        url = server.url
        server.close()
        result = helpdesk.send(helpdesk.build_request("Wi-Fi does not work after installing"), url, timeout=3)
        self.assertEqual((False, "network"), (result.ok, result.error))


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.file = Path(self.temp.name) / "Quay-man-hinh.mp4"
        self.file.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"v" * 200_000)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def upload(self, server: UploadServer, mime: str = "video/mp4", **options) -> helpdesk.UploadResult:
        try:
            return helpdesk.upload("1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", self.file, mime, base=server.url,
                                   timeout=5, **options)
        finally:
            server.close()

    def test_a_file_goes_to_the_storage_link_then_is_complete(self) -> None:
        server = UploadServer()
        sent: list[int] = []
        result = self.upload(server, duration=60.96, on_progress=sent.append)
        self.assertEqual((True, "a1"), (result.ok, result.attachment_id))
        (_m1, path1, headers1, body1), (method2, path2, headers2, body2), (_m3, path3, _h3, body3) = server.calls
        self.assertEqual("/api/caramos/help/v1/attachments", path1)
        self.assertEqual({"schema": 1, "upload_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed", "mime": "video/mp4",
                          "size": self.file.stat().st_size, "duration": 60.9}, json.loads(body1))
        self.assertTrue(headers1["user-agent"].startswith("caramos-help/"))
        # The file itself, with the signed type and its length: never chunked.
        self.assertEqual(("PUT", "/storage/a1?sig=x", self.file.read_bytes()), (method2, path2, body2))
        self.assertEqual("video/mp4", headers2["content-type"])
        self.assertEqual(str(self.file.stat().st_size), headers2["content-length"])
        self.assertNotIn("transfer-encoding", headers2)
        self.assertEqual("/api/caramos/help/v1/attachments/a1/complete", path3)
        self.assertEqual({"upload_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"}, json.loads(body3))
        self.assertEqual(self.file.stat().st_size, sent[-1])

    def test_pictures_have_no_length(self) -> None:
        server = UploadServer()
        self.assertTrue(self.upload(server, "image/webp").ok)
        self.assertNotIn("duration", json.loads(server.calls[0][3]))

    def test_answers_of_the_api(self) -> None:
        self.assertEqual("unavailable", self.upload(UploadServer(presign=404)).error)   # VNLF has not built it yet
        limited = self.upload(UploadServer(presign=429))
        self.assertEqual(("rate-limited", 3600), (limited.error, limited.retry_after))
        self.assertEqual("rejected", self.upload(UploadServer(presign=422)).error)
        self.assertEqual("server", self.upload(UploadServer(presign=503)).error)
        self.assertEqual("rejected", self.upload(UploadServer(complete=409)).error)

    def test_the_storage_refuses(self) -> None:
        self.assertEqual("rejected", self.upload(UploadServer(put=403)).error)   # an expired link
        self.assertEqual("server", self.upload(UploadServer(put=500)).error)

    def test_files_go_to_https_links_only(self) -> None:
        for link in ("http://example.com/storage/a1", "javascript:alert(1)", None):
            server = UploadServer(answer={"id": "a1", "upload_url": link, "headers": {}})
            self.assertEqual("server", self.upload(server).error)
            self.assertEqual(["POST"], [call[0] for call in server.calls])
        self.assertTrue(helpdesk._upload_target("https://bucket.r2.cloudflarestorage.com/x?X-Amz-Signature=1"))

    def test_no_connection(self) -> None:
        server = UploadServer()
        server.close()
        result = helpdesk.upload("u", self.file, "video/mp4", base=server.url, timeout=3)
        self.assertEqual((False, "network"), (result.ok, result.error))


class HistoryTests(unittest.TestCase):
    """Requests VNLF accepted are kept so the panel applet can open their pages again."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "caramos-help" / "requests.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def sent(self, request_id: str, page: str, kind: str = "bug", message: str = "The Update Center stops halfway",
             public: bool = False) -> bool:
        payload = helpdesk.build_request(message, kind=kind, public=public)
        when = datetime(2026, 10, 12, 21, 30, tzinfo=timezone(timedelta(hours=7)))
        result = helpdesk.SendResult(True, request_id=request_id, url=f"https://vietnamlinuxfamily.net/caramos/help/{page}")
        return helpdesk.remember(result, payload, self.path, now=when)

    def ids(self) -> list[str]:
        return [item["id"] for item in helpdesk.sent_requests(self.path)]

    def test_accepted_requests_are_kept_newest_first(self) -> None:
        self.assertTrue(self.sent("H-1", "a"))
        self.assertTrue(self.sent("H-2", "b", kind="feedback"))
        self.assertEqual(["H-2", "H-1"], self.ids())
        self.assertEqual({"id": "H-1", "url": "https://vietnamlinuxfamily.net/caramos/help/a", "type": "bug",
                          "sent": "2026-10-12T21:30:00+07:00", "message": "The Update Center stops halfway",
                          "public": False},
                         helpdesk.sent_requests(self.path)[1])
        # Anyone with a page link can read the request: the file is the user's only.
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual([self.path.name], os.listdir(self.path.parent))

    def test_a_request_answered_twice_is_kept_once(self) -> None:
        self.sent("H-1", "a")
        self.sent("H-2", "b")
        self.sent("H-1", "a")   # VNLF's answer to the same message sent again
        self.assertEqual(["H-1", "H-2"], self.ids())

    def test_the_applet_knows_which_requests_were_shared_with_the_community(self) -> None:
        self.sent("H-1", "a", public=True)
        self.sent("H-2", "b")
        self.assertEqual([("H-2", False), ("H-1", True)],
                         [(item["id"], item["public"]) for item in helpdesk.sent_requests(self.path)])

    def test_a_request_sent_again_keeps_the_first_choice_of_sharing(self) -> None:
        # VNLF answers it with the first request's page and keeps that one's choice.
        self.sent("H-1", "a")
        self.sent("H-1", "a", public=True)
        self.assertEqual([("H-1", False)], [(item["id"], item["public"]) for item in helpdesk.sent_requests(self.path)])
        self.sent("H-2", "b", public=True)
        self.sent("H-2", "b")
        self.assertEqual([("H-2", True), ("H-1", False)],
                         [(item["id"], item["public"]) for item in helpdesk.sent_requests(self.path)])

    def test_only_the_last_requests_are_kept(self) -> None:
        for number in range(helpdesk.HISTORY_LIMIT + 5):
            self.sent(f"H-{number}", str(number))
        self.assertEqual(helpdesk.HISTORY_LIMIT, len(self.ids()))
        self.assertEqual(f"H-{helpdesk.HISTORY_LIMIT + 4}", self.ids()[0])

    def test_the_start_of_the_message_is_kept_on_one_line(self) -> None:
        self.sent("H-1", "a", message="Line one\n\nline   two " + "x" * 200)
        message = helpdesk.sent_requests(self.path)[0]["message"]
        self.assertTrue(message.startswith("Line one line two x"))
        self.assertEqual(120, len(message))

    def test_failed_requests_and_other_links_are_not_kept(self) -> None:
        payload = helpdesk.build_request("The Update Center stops halfway")
        self.assertFalse(helpdesk.remember(helpdesk.SendResult(False, "network"), payload, self.path))
        self.assertFalse(helpdesk.remember(helpdesk.SendResult(True, request_id="H-1"), payload, self.path))
        self.assertEqual([], helpdesk.sent_requests(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"requests": [{"id": "H-9", "url": "javascript:alert(1)"}, "x"]}', encoding="utf-8")
        self.assertEqual([], helpdesk.sent_requests(self.path))
        self.path.write_text("{broken", encoding="utf-8")
        self.assertEqual([], helpdesk.sent_requests(self.path))
        self.assertTrue(self.sent("H-1", "a"))   # a broken file is replaced

    def test_the_file_is_in_the_state_directory(self) -> None:
        self.assertEqual(Path("/s/caramos-help/requests.json"),
                         helpdesk.history_path({"XDG_STATE_HOME": "/s", "HOME": "/h"}))
        self.assertEqual(Path("/h/.local/state/caramos-help/requests.json"), helpdesk.history_path({"HOME": "/h"}))


class StatusTests(unittest.TestCase):
    """What VNLF says about a request: GET /requests/<token> of the help API."""

    def serve(self, answers: dict[str, tuple[int, dict]]) -> StatusServer:
        server = StatusServer(answers)
        self.addCleanup(server.close)
        return server

    def test_it_is_asked_where_the_requests_go(self) -> None:
        self.assertEqual(f"{helpdesk.HELP_URL}/requests/{TOKEN}", helpdesk.status_url(PAGE))
        self.assertEqual(f"http://127.0.0.1:8765/help/requests/{TOKEN}",
                         helpdesk.status_url(PAGE, "http://127.0.0.1:8765/help/"))
        for page in ("https://vietnamlinuxfamily.net/", f"http://vietnamlinuxfamily.net/caramos/help/{TOKEN}",
                     f"{PAGE}/x", f"{PAGE}?x=1", "https://vietnamlinuxfamily.net/caramos/help/a%2Fb",
                     "https://vietnamlinuxfamily.net/caramos/help/test1043",   # VNLF refuses tokens under 20
                     "https://vietnamlinuxfamily.net/caramos/help/" + "a" * 65):
            with self.subTest(page=page):
                self.assertEqual("", helpdesk.status_url(page))
        self.assertEqual(helpdesk.Status(False, "rejected"), helpdesk.fetch_status("https://vietnamlinuxfamily.net/"))

    def test_an_answered_request(self) -> None:
        public = "https://vietnamlinuxfamily.net/caramos/H-1001"
        server = self.serve({TOKEN: (200, status_answer("RESOLVED", reply="Cài lại gói firmware rồi khởi động lại.",
                                                        consent=True, url=public,
                                                        resolved="2026-10-15T02:00:00.000Z", answers=2))})
        self.assertEqual(helpdesk.Status(True, status="RESOLVED", replied=True, public=True, public_url=public,
                                         finished="2026-10-15T02:00:00.000Z", answers=2),
                         helpdesk.fetch_status(PAGE, server.url))
        path, headers = server.calls[0]
        self.assertEqual(f"/api/caramos/help/v1/requests/{TOKEN}", path)
        self.assertTrue(headers["User-Agent"].startswith("caramos-help/"))

    def test_an_open_request(self) -> None:
        answer = status_answer("IN_REVIEW", reply="  ", resolved=None)
        del answer["public"], answer["answers"]   # VNLF before docs 52
        server = self.serve({TOKEN: (200, answer)})
        self.assertEqual(helpdesk.Status(True, status="IN_REVIEW"), helpdesk.fetch_status(PAGE, server.url))

    def test_a_request_vnlf_removed_is_gone(self) -> None:
        server = self.serve({})
        self.assertEqual(helpdesk.Status(True, status=helpdesk.GONE), helpdesk.fetch_status(PAGE, server.url))

    def test_other_answers_say_nothing_about_the_request(self) -> None:
        # A 404 that is not VNLF's own (no API at that address), errors and unknown states.
        server = self.serve({TOKEN: (404, {"message": "NOT_FOUND"}), "b" * 43: (500, {}),
                             "c" * 43: (200, status_answer("SPAM"))})
        base = "https://vietnamlinuxfamily.net/caramos/help/"
        self.assertEqual(helpdesk.Status(False, "unavailable"), helpdesk.fetch_status(PAGE, server.url))
        self.assertEqual(helpdesk.Status(False, "server"), helpdesk.fetch_status(base + "b" * 43, server.url))
        self.assertEqual(helpdesk.Status(False, "server"), helpdesk.fetch_status(base + "c" * 43, server.url))
        self.assertEqual(helpdesk.Status(False, "network"), helpdesk.fetch_status(PAGE, "http://127.0.0.1:9/x"))


class RefreshTests(unittest.TestCase):
    """VNLF's word on each request is kept with it, for the panel applet and the window's list."""

    NOW = datetime(2026, 11, 20, 10, 0, tzinfo=timezone(timedelta(hours=7)))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "caramos-help" / "requests.json"
        self.asked: list[str] = []
        self.statuses: dict[str, helpdesk.Status] = {}

    @staticmethod
    def page(name: str) -> str:
        return f"https://vietnamlinuxfamily.net/caramos/help/{name}"

    def keep(self, *items: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"requests": list(items)}), encoding="utf-8")

    def fetch(self, page: str, _base: str | None) -> helpdesk.Status:
        self.asked.append(page)
        return self.statuses.get(page, helpdesk.Status(False, "network"))

    def refresh(self) -> helpdesk.Refresh:
        return helpdesk.refresh(self.path, now=self.NOW, fetch=self.fetch)

    def days_ago(self, days: int) -> str:
        return (self.NOW - timedelta(days=days)).isoformat(timespec="seconds")

    def test_answers_are_kept_and_told_once(self) -> None:
        self.keep({"id": "H-2", "url": self.page("b"), "public": True},
                  {"id": "H-1", "url": self.page("a"), "public": False})
        self.statuses = {self.page("b"): helpdesk.Status(True, status="IN_REVIEW", replied=True, public=False),
                         self.page("a"): helpdesk.Status(True, status="OPEN")}
        result = self.refresh()
        self.assertEqual((2, 0), (result.checked, result.failed))
        self.assertEqual([("H-2", True, 0)], [(news.request["id"], news.reply, news.answers) for news in result.news])
        self.assertEqual([
            # The user stopped sharing it on the request's page.
            {"id": "H-2", "url": self.page("b"), "public": False, "status": "IN_REVIEW", "replied": True},
            {"id": "H-1", "url": self.page("a"), "public": False, "status": "OPEN", "replied": False},
        ], helpdesk.sent_requests(self.path))
        self.assertEqual((), self.refresh().news)   # told already
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual([self.path.name], os.listdir(self.path.parent))

    def test_finished_requests_and_their_community_page(self) -> None:
        public = "https://vietnamlinuxfamily.net/caramos/H-1"
        self.keep({"id": "H-1", "url": self.page("a"), "public": True})
        self.statuses = {self.page("a"): helpdesk.Status(True, status="RESOLVED", replied=True, public=True,
                                                         public_url=public, finished="2026-11-19T02:00:00.000Z",
                                                         answers=2)}
        self.refresh()
        item = helpdesk.sent_requests(self.path)[0]
        self.assertEqual(("RESOLVED", "2026-11-19T02:00:00.000Z", public, 2),
                         (item["status"], item["finished"], item["public_url"], item["answers"]))
        # Opened again, and the sender stopped sharing: the public page and its answers are gone.
        self.statuses = {self.page("a"): helpdesk.Status(True, status="OPEN", replied=True, public=False)}
        self.refresh()
        item = helpdesk.sent_requests(self.path)[0]
        self.assertEqual(("OPEN", False), (item["status"], item["public"]))
        for gone in ("finished", "public_url", "answers"):
            self.assertNotIn(gone, item)

    def test_new_answers_from_the_community_are_told(self) -> None:
        self.keep({"id": "H-1", "url": self.page("a"), "public": True, "status": "OPEN", "replied": True})

        def answers(count: int) -> tuple[tuple[str, bool, int], ...]:
            self.statuses = {self.page("a"): helpdesk.Status(True, status="OPEN", replied=True, public=True,
                                                             answers=count)}
            return tuple((news.request["id"], news.reply, news.answers) for news in self.refresh().news)

        self.assertEqual((("H-1", False, 2),), answers(2))
        self.assertEqual((), answers(2))
        self.assertEqual((), answers(1))   # a moderator hid one
        self.assertEqual(1, helpdesk.sent_requests(self.path)[0]["answers"])
        self.assertEqual((("H-1", False, 2),), answers(3))

    def test_the_menu_lists_a_finished_request_for_30_days(self) -> None:
        self.assertEqual(30, helpdesk.KEEP_FINISHED_DAYS)
        cases = [
            ({"status": "OPEN", "sent": self.days_ago(200)}, True),
            ({"status": "IN_REVIEW"}, True),
            ({}, True),
            ({"status": "RESOLVED", "finished": self.days_ago(29)}, True),
            ({"status": "RESOLVED", "finished": self.days_ago(31)}, False),
            ({"status": "CLOSED", "finished": self.days_ago(31)}, False),
            ({"status": "GONE", "finished": self.days_ago(31)}, False),
            ({"status": "CLOSED", "finished": "not a time"}, True),
            ({"status": "CLOSED"}, True),
        ]
        for item, listed in cases:
            with self.subTest(item=item):
                self.assertEqual(listed, helpdesk.listed(item, self.NOW))

    def test_only_requests_that_may_still_change_are_asked_about(self) -> None:
        self.keep({"id": "H-5", "url": self.page("e"), "status": "OPEN"},
                  {"id": "H-4", "url": self.page("d"), "status": "RESOLVED", "finished": self.days_ago(10)},
                  {"id": "H-3", "url": self.page("c"), "status": "RESOLVED", "finished": self.days_ago(40)},
                  {"id": "H-2", "url": self.page("b"), "status": "CLOSED", "finished": self.days_ago(1)},
                  {"id": "H-1", "url": self.page("a"), "status": "GONE", "finished": self.days_ago(1)})
        self.refresh()
        self.assertEqual([self.page("e"), self.page("d")], self.asked)
        self.keep({"id": "H-2", "url": self.page("b"), "status": "CLOSED", "finished": self.days_ago(1)})
        before = self.path.read_bytes()
        self.asked.clear()
        self.assertEqual(helpdesk.Refresh(), self.refresh())
        self.assertEqual([], self.asked)
        self.assertEqual(before, self.path.read_bytes())

    def test_a_removed_request_is_gone_since_it_was_first_missed(self) -> None:
        self.keep({"id": "H-1", "url": self.page("a"), "status": "OPEN", "replied": True})
        self.statuses = {self.page("a"): helpdesk.Status(True, status=helpdesk.GONE)}
        self.assertEqual((), self.refresh().news)
        item = helpdesk.sent_requests(self.path)[0]
        self.assertEqual(("GONE", "2026-11-20T10:00:00+07:00"), (item["status"], item["finished"]))
        self.asked.clear()
        self.refresh()
        self.assertEqual([], self.asked)

    def test_without_an_answer_nothing_changes(self) -> None:
        kept = {"id": "H-1", "url": self.page("a"), "status": "IN_REVIEW", "replied": True, "public": True}
        self.keep(kept)
        result = self.refresh()
        self.assertEqual((1, 1, ()), (result.checked, result.failed, result.news))
        self.assertEqual([kept], helpdesk.sent_requests(self.path))

    def test_a_request_sent_while_vnlf_is_asked_stays(self) -> None:
        self.keep({"id": "H-1", "url": self.page("a")})
        self.statuses = {self.page("a"): helpdesk.Status(True, status="OPEN")}

        def fetch(page: str, base: str | None) -> helpdesk.Status:
            payload = helpdesk.build_request("The Update Center stops halfway", kind="bug")
            helpdesk.remember(helpdesk.SendResult(True, request_id="H-2", url=self.page("b")), payload, self.path)
            return self.fetch(page, base)

        helpdesk.refresh(self.path, now=self.NOW, fetch=fetch)
        self.assertEqual([("H-2", None), ("H-1", "OPEN")],
                         [(item["id"], item.get("status")) for item in helpdesk.sent_requests(self.path)])

    def test_a_request_sent_again_keeps_what_vnlf_said(self) -> None:
        self.keep({"id": "H-1", "url": self.page("a"), "public": True, "status": "IN_REVIEW", "replied": True})
        payload = helpdesk.build_request("The Update Center stops halfway", kind="bug")
        helpdesk.remember(helpdesk.SendResult(True, request_id="H-1", url=self.page("a")), payload, self.path)
        item = helpdesk.sent_requests(self.path)[0]
        self.assertEqual(("IN_REVIEW", True, True), (item["status"], item["replied"], item["public"]))


class AnswerNotificationTests(unittest.TestCase):
    def test_one_answer_names_the_request_and_who_answered(self) -> None:
        from caramos_driver_check import notify

        request = {"id": "H-1043", "message": "The speakers crackle", "url": PAGE}
        title, body = notify.news_text([helpdesk.News(request, reply=True, answers=1)])
        self.assertEqual("VNLF answered your request H-1043", title)
        self.assertEqual("The speakers crackle", body)
        title, _body = notify.news_text([helpdesk.News(request, answers=2)])
        self.assertEqual("The community answered your request H-1043", title)

    def test_answers_to_several_requests_are_one_notification(self) -> None:
        from caramos_driver_check import notify

        title, body = notify.news_text([helpdesk.News({"id": "H-2", "url": PAGE}, reply=True),
                                        helpdesk.News({"id": "H-1", "url": PAGE}, answers=1)])
        self.assertEqual("New answers to 2 of your requests", title)
        self.assertEqual("H-2, H-1", body)
        self.assertGreater(notify.REPLY_WAIT_SECONDS, 0)


class ApiDocumentTests(unittest.TestCase):
    def test_the_api_is_documented_for_the_vnlf_server(self) -> None:
        document = (ROOT / "HELP_API.md").read_text(encoding="utf-8")
        self.assertIn(helpdesk.HELP_URL, document)
        for field in ("schema", "type", "message", "contact", "probe_url", "report", "system", "client", "retry_after",
                      "upload_id", "attachments", "mime", "size", "duration", "upload_url", "headers", "public",
                      "consent", "answers", "answer_id", "author", "accepted"):
            self.assertIn(f'"{field}"', document)
        self.assertIn("/api/caramos/help/v1/attachments", document)


if __name__ == "__main__":
    unittest.main()
