import json
import io
import os
import queue
import socket
import tempfile
import threading
import zipfile
import unittest
from datetime import datetime
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

import tools_calendar
import tools_notes
import tools_reminders
import tools_instance
import tools_artifacts
import tools_creator
import tools_uploads
import tools_auth
import tools_settings
import dashboard


class CalendarIntegrationTests(unittest.TestCase):
    def test_macos_event_uses_calendar_and_returns_event_id(self):
        with patch.object(tools_calendar.platform, "system", return_value="Darwin"), \
                patch.object(tools_calendar, "run_applescript", return_value="event-123") as run_script:
            result = tools_calendar.schedule_meeting("Planning", "2030-06-01 10:30", 45)

        self.assertIn("Calendar.app", result)
        self.assertIn("event-123", result)
        script = run_script.call_args.args[0]
        self.assertIn("start date of newEvent is not eventStart", script)
        self.assertNotIn("count of (events of targetCalendar", script)

    def test_macos_calendar_permission_failure_is_not_success(self):
        with patch.object(tools_calendar.platform, "system", return_value="Darwin"), \
                patch.object(tools_calendar, "run_applescript", side_effect=RuntimeError("Automation denied")):
            result = tools_calendar.schedule_meeting("Planning", "2030-06-01 10:30")

        self.assertTrue(result.startswith("Failed"))


class DashboardUploadTests(unittest.TestCase):
    def test_dashboard_submission_is_logged_before_it_is_queued(self):
        inbound = queue.Queue()
        message = {
            "text": "Make the video with this song",
            "attachments": [{"name": "goochi-Darling.mp3", "mime_type": "audio/mpeg"}],
        }
        with patch.object(dashboard, "_get_inbound_queue", return_value=inbound), \
                patch.object(dashboard, "add_log") as add_log:
            dashboard.submit_user_message(message)

        add_log.assert_called_once_with("You", "Make the video with this song\n[Attached: goochi-Darling.mp3]")
        queued = inbound.get_nowait()
        self.assertTrue(queued["_dashboard_logged"])
        self.assertEqual(queued["text"], "Make the video with this song")

    def test_chat_file_picker_accepts_mp3(self):
        self.assertIn('accept=".txt', dashboard._HTML_PAGE)
        file_input = dashboard._HTML_PAGE.split('id="fileInput"', 1)[1].split(">", 1)[0]
        self.assertIn(".mp3", file_input)
        self.assertIn("audio/mpeg", file_input)
        self.assertIn(".pdf", file_input)
        self.assertIn(".docx", file_input)
        self.assertIn(".epub", file_input)

    def test_multipart_upload_extracts_text_and_accepts_video(self):
        boundary = "test-boundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="text"\r\n\r\n'
            'Please summarize these files\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="notes.txt"\r\n'
            'Content-Type: text/plain\r\n\r\nMeeting notes\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="clip.mp4"\r\n'
            'Content-Type: video/mp4\r\n\r\nvideo bytes\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="voice.mp3"\r\n'
            'Content-Type: audio/mpeg\r\n\r\naudio bytes\r\n'
            f'--{boundary}--\r\n'
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_uploads, "UPLOAD_DIR", temp_dir):
            text, attachments = tools_uploads.parse_multipart_form(
                f"multipart/form-data; boundary={boundary}", body
            )

        self.assertEqual(text, "Please summarize these files")
        self.assertEqual(attachments[0]["text"], "Meeting notes")
        self.assertEqual(attachments[1]["mime_type"], "video/mp4")
        self.assertEqual(attachments[2]["mime_type"], "audio/mpeg")
        attachments[2]["transcript"] = "The appointment is at noon."
        description = tools_uploads.describe_attachments(attachments)
        self.assertIn("Meeting notes", description)
        self.assertIn("cannot inspect video frames", description)
        self.assertIn("The appointment is at noon", description)

    def test_multipart_upload_accepts_web_shortcut(self):
        boundary = "link-boundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="clip.url"\r\n'
            'Content-Type: text/plain\r\n\r\n[InternetShortcut]\r\nURL=https://example.com/clip\r\n'
            f'--{boundary}--\r\n'
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_uploads, "UPLOAD_DIR", temp_dir):
            _, attachments = tools_uploads.parse_multipart_form(
                f"multipart/form-data; boundary={boundary}", body
            )

        self.assertEqual(attachments[0]["url"], "https://example.com/clip")
        self.assertIn("https://example.com/clip", tools_uploads.describe_attachments(attachments))

    def test_dashboard_multipart_endpoint_queues_uploaded_content(self):
        boundary = "http-boundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="text"\r\n\r\nDescribe this\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="clip.mp4"\r\n'
            'Content-Type: video/mp4\r\n\r\nvideo bytes\r\n'
            f'--{boundary}--\r\n'
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_uploads, "UPLOAD_DIR", temp_dir), \
            patch.object(dashboard, "submit_user_message") as submit, \
            patch.object(tools_auth, "is_authenticated", return_value=True):
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port)
                connection.request(
                    "POST", "/api/send", body,
                    {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                )
                response = connection.getresponse()
                response_body = response.read()
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        self.assertEqual(response.status, 200)
        self.assertIn(b'"ok":true', response_body)
        submitted = submit.call_args.args[0]
        self.assertEqual(submitted["text"], "Describe this")
        self.assertEqual(submitted["attachments"][0]["mime_type"], "video/mp4")


class DocumentExtractionTests(unittest.TestCase):
    def test_pdf_upload_extracts_page_text(self):
        from unittest.mock import Mock
        import pypdf

        reader = Mock(pages=[Mock(extract_text=Mock(return_value="A book's first page."))])
        with patch.object(pypdf, "PdfReader", return_value=reader):
            text = tools_uploads._extract_document_text(".pdf", b"pdf bytes", "book.pdf")

        self.assertIn("[Page 1]", text)
        self.assertIn("A book's first page", text)

    def test_docx_and_epub_uploads_extract_readable_text(self):
        from docx import Document

        word_buffer = io.BytesIO()
        document = Document()
        document.add_paragraph("A chapter about memory.")
        document.save(word_buffer)
        word_text = tools_uploads._extract_document_text(".docx", word_buffer.getvalue(), "book.docx")

        epub_buffer = io.BytesIO()
        with zipfile.ZipFile(epub_buffer, "w") as book:
            book.writestr("chapter.xhtml", "<html><body><h1>Chapter One</h1><p>Readable prose.</p></body></html>")
        epub_text = tools_uploads._extract_document_text(".epub", epub_buffer.getvalue(), "book.epub")

        self.assertIn("A chapter about memory", word_text)
        self.assertIn("Chapter One", epub_text)
        self.assertIn("Readable prose", epub_text)

    def test_scanned_pdf_reports_ocr_limitation(self):
        from unittest.mock import Mock
        import pypdf

        reader = Mock(pages=[Mock(extract_text=Mock(return_value=""))])
        with patch.object(pypdf, "PdfReader", return_value=reader):
            with self.assertRaisesRegex(ValueError, "scanned PDFs need OCR"):
                tools_uploads._extract_document_text(".pdf", b"pdf bytes", "scan.pdf")


class LocalDashboardAuthTests(unittest.TestCase):
    def test_remember_me_styles_are_not_rendered_as_signin_text(self):
        stylesheet = dashboard._HTML_PAGE.split("<style>", 1)[1].split("</style>", 1)[0]
        page_body = dashboard._HTML_PAGE.split("</style>", 1)[1]
        self.assertIn(".rememberControl {", stylesheet)
        self.assertIn(".rememberControl input {", stylesheet)
        self.assertNotIn(".rememberControl {", page_body)
        self.assertIn("#authGate { position:fixed; z-index:1000", stylesheet)
        self.assertIn("align-items:flex-start; justify-content:center", stylesheet)
        self.assertIn("margin:auto 0", stylesheet)
        self.assertIn("overflow-y:auto", stylesheet)

    def test_dashboard_requires_login_and_accepts_local_signup(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_auth, "DATA_DIR", temp_dir), \
                patch.object(tools_auth, "ACCOUNT_FILE", os.path.join(temp_dir, "account.json")), \
                patch.object(dashboard.tools_profile, "set_name") as set_name:
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port)
                connection.request("GET", "/api/log")
                denied = connection.getresponse()
                denied.read()
                self.assertEqual(denied.status, 401)

                origin = f"http://127.0.0.1:{server.server_port}"
                connection.request(
                    "POST", "/api/auth/signup",
                    json.dumps({"name": "Avery Example", "identifier": "avery@example.com",
                                "password": "local-test-password"}),
                    {"Content-Type": "application/json", "Origin": origin},
                )
                signed_up = connection.getresponse()
                signup_body = json.loads(signed_up.read())
                set_cookie = signed_up.getheader("Set-Cookie")
                cookie = set_cookie.split(";", 1)[0]
                self.assertIn("Max-Age=2592000", set_cookie)
                self.assertEqual(signed_up.status, 200)
                self.assertEqual(signup_body["account"]["name"], "Avery Example")
                set_name.assert_called_once_with("Avery Example")

                connection.request("GET", "/api/log", headers={"Cookie": cookie})
                allowed = connection.getresponse()
                allowed.read()
                self.assertEqual(allowed.status, 200)

                connection.request(
                    "POST", "/api/auth/logout", "{}",
                    {"Content-Type": "application/json", "Origin": origin, "Cookie": cookie},
                )
                logged_out = connection.getresponse()
                logged_out.read()
                connection.request("GET", "/api/log", headers={"Cookie": cookie})
                denied_again = connection.getresponse()
                denied_again.read()
                self.assertEqual(denied_again.status, 401)

                connection.request(
                    "POST", "/api/auth/login",
                    json.dumps({"identifier": "avery@example.com", "password": "wrong-password"}),
                    {"Content-Type": "application/json", "Origin": origin},
                )
                bad_login = connection.getresponse()
                bad_login.read()
                self.assertEqual(bad_login.status, 400)

                connection.request(
                    "POST", "/api/auth/login",
                    json.dumps({"identifier": "avery@example.com", "password": "local-test-password",
                                "remember": False}),
                    {"Content-Type": "application/json", "Origin": origin},
                )
                logged_in = connection.getresponse()
                login_body = json.loads(logged_in.read())
                login_cookie = logged_in.getheader("Set-Cookie").split(";", 1)[0]
                self.assertEqual(logged_in.status, 200)
                self.assertEqual(login_body["account"]["name"], "Avery Example")
                self.assertNotIn("max-age=", logged_in.getheader("Set-Cookie").lower())
                connection.request("GET", "/api/log", headers={"Cookie": login_cookie})
                reauthenticated = connection.getresponse()
                reauthenticated.read()
                self.assertEqual(reauthenticated.status, 200)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_settings_uses_nested_social_menu_and_one_step_back_stack(self):
        self.assertIn('id="settingsBack" type="button" hidden', dashboard._HTML_PAGE)
        self.assertIn('#settingsBack[hidden] { display:none; }', dashboard._HTML_PAGE)
        self.assertIn("'Link socials'", dashboard._HTML_PAGE)
        self.assertIn("data-screen=\"socials\"", dashboard._HTML_PAGE)
        self.assertIn("data-screen=\"social:${escAttr(platform)}\"", dashboard._HTML_PAGE)
        self.assertIn("settingsStack.push(screen);", dashboard._HTML_PAGE)
        self.assertIn("settingsStack.pop();", dashboard._HTML_PAGE)
        self.assertIn("settingsInitialized = true;", dashboard._HTML_PAGE)
        self.assertIn("menuCard('agents', 'AI', 'Connect agents'", dashboard._HTML_PAGE)
        self.assertIn("Search providers or model names", dashboard._HTML_PAGE)
        self.assertIn("Provider charges and data policies apply", dashboard._HTML_PAGE)

    def test_current_conversation_navigation_does_not_start_a_new_chat(self):
        self.assertIn('data-action="current-chat"', dashboard._HTML_PAGE)
        self.assertIn('data-action="new-chat"', dashboard._HTML_PAGE)
        self.assertIn("if (btn.dataset.action === 'new-chat') startNewChat();", dashboard._HTML_PAGE)

    def test_chat_refresh_preserves_scroll_when_reader_is_above_bottom(self):
        self.assertIn("const shouldFollow = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 80;",
                      dashboard._HTML_PAGE)
        self.assertIn("const previousScrollTop = logEl.scrollTop;", dashboard._HTML_PAGE)
        self.assertIn("logEl.scrollTop = shouldFollow ? logEl.scrollHeight : previousScrollTop;",
                      dashboard._HTML_PAGE)

    def test_signed_remembered_session_survives_restart_and_logout_revokes_it(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_auth, "DATA_DIR", temp_dir), \
                patch.object(tools_auth, "ACCOUNT_FILE", os.path.join(temp_dir, "account.json")):
            token, _ = tools_auth.signup("Avery Example", "avery@example.com", "local-test-password")
            tools_auth._sessions.clear()
            self.assertTrue(tools_auth.is_authenticated(token))
            tools_auth.logout(token)
            self.assertFalse(tools_auth.is_authenticated(token))

    def test_remembered_session_expires_even_while_process_is_running(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_auth, "DATA_DIR", temp_dir), \
                patch.object(tools_auth, "ACCOUNT_FILE", os.path.join(temp_dir, "account.json")):
            token, _ = tools_auth.signup("Avery Example", "avery@example.com", "local-test-password")
            expired_time = tools_auth.time.time() + tools_auth.SESSION_TTL_SECONDS + 1
            with patch.object(tools_auth.time, "time", return_value=expired_time):
                self.assertFalse(tools_auth.is_authenticated(token))
    def test_settings_save_returns_masked_success_and_rejects_unknown_key(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_settings, "SETTINGS_FILE", os.path.join(temp_dir, "settings.json")), \
                patch.object(tools_auth, "is_authenticated", return_value=True):
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port)
                headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.server_port}"}
                connection.request("POST", "/api/settings",
                                   json.dumps({"key": "BLUESKY_IDENTIFIER", "value": "person@example.com"}), headers)
                saved = connection.getresponse()
                result = json.loads(saved.read())
                self.assertEqual(saved.status, 200)
                self.assertIn("Saved BLUESKY_IDENTIFIER", result["message"])
                self.assertNotIn("person@example.com", result["masked"])

                connection.request("GET", "/api/settings")
                providers = json.loads(connection.getresponse().read())["agents"]
                self.assertEqual({provider["name"] for provider in providers},
                                 {"Claude", "ChatGPT", "Gemini", "Grok", "Groq"})

                connection.request("POST", "/api/settings",
                                   json.dumps({"key": "UNEXPECTED_KEY", "value": "secret"}), headers)
                rejected = connection.getresponse()
                rejection = json.loads(rejected.read())
                self.assertEqual(rejected.status, 400)
                self.assertIn("Unsupported settings key", rejection["error"])
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_credential_save_button_has_visible_status_feedback(self):
        self.assertIn("data-save-setting=", dashboard._HTML_PAGE)
        self.assertIn("class=\"settingSave\"", dashboard._HTML_PAGE)
        self.assertIn("className = 'settingFeedback success'", dashboard._HTML_PAGE)

    def test_signin_page_can_switch_modes_and_toggle_password_visibility(self):
        self.assertIn('id="authModeToggle"', dashboard._HTML_PAGE)
        self.assertIn("authMode = authMode === 'login' ? 'register' : 'login';", dashboard._HTML_PAGE)
        self.assertIn('data-toggle-password="authPassword"', dashboard._HTML_PAGE)
        self.assertIn('.authPanel .passwordToggle { width:auto; margin:0; }', dashboard._HTML_PAGE)
        self.assertIn("input.type = visible ? 'text' : 'password';", dashboard._HTML_PAGE)

    def test_profile_and_password_are_separate_settings_screens(self):
        self.assertIn("menuCard('profile', 'P', 'Profile'", dashboard._HTML_PAGE)
        self.assertIn("menuCard('password', '*', 'Password'", dashboard._HTML_PAGE)
        self.assertIn('data-screen="profile"', dashboard._HTML_PAGE)
        self.assertIn('data-screen="password"', dashboard._HTML_PAGE)
        self.assertIn('id="saveProfile"', dashboard._HTML_PAGE)
        self.assertIn('id="savePassword"', dashboard._HTML_PAGE)

    def test_profile_and_password_routes_update_local_account_securely(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_auth, "DATA_DIR", temp_dir), \
                patch.object(tools_auth, "ACCOUNT_FILE", os.path.join(temp_dir, "account.json")), \
                patch.object(tools_auth, "is_authenticated", return_value=True), \
                patch.object(dashboard.tools_profile, "set_name") as set_name:
            tools_auth.signup("Avery Example", "avery@example.com", "local-test-password")
            server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_port)
                headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{server.server_port}"}
                connection.request("POST", "/api/account/profile",
                                   json.dumps({"name": "Avery North", "identifier": "avery.north@example.com"}), headers)
                profile_response = connection.getresponse()
                profile_result = json.loads(profile_response.read())
                self.assertEqual(profile_response.status, 200)
                self.assertEqual(profile_result["account"]["name"], "Avery North")
                set_name.assert_called_once_with("Avery North")

                connection.request("POST", "/api/account/password",
                                   json.dumps({"current_password": "incorrect-password",
                                               "new_password": "a-new-local-password"}), headers)
                rejected = connection.getresponse()
                self.assertEqual(rejected.status, 400)
                rejected.read()

                connection.request("POST", "/api/account/password",
                                   json.dumps({"current_password": "local-test-password",
                                               "new_password": "a-new-local-password"}), headers)
                changed = connection.getresponse()
                changed_result = json.loads(changed.read())
                self.assertEqual(changed.status, 200)
                self.assertEqual(set(changed_result), {"ok", "message"})
                self.assertNotIn("a-new-local-password", json.dumps(changed_result))
                _, account = tools_auth.login("avery.north@example.com", "a-new-local-password")
                self.assertEqual(account["name"], "Avery North")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class CreatorArtifactTests(unittest.TestCase):
    def test_comfyui_video_generation_does_not_require_image_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "generated.mp4")
            manifest = os.path.join(temp_dir, "artifacts.json")
            with open(source, "wb") as generated_video:
                generated_video.write(b"generated video")

            hooks = {**tools_creator._hooks, "image": None}
            with patch.object(tools_creator, "OUTPUT_DIR", temp_dir), \
                    patch.object(tools_creator, "_media_tool_path", return_value="/usr/local/bin/ffmpeg"), \
                    patch.object(tools_creator, "_comfyui_workflow_configured", return_value=True), \
                    patch.object(tools_creator, "_comfyui_generate_video", return_value=source), \
                    patch.object(tools_creator, "_probe", return_value={"duration": 12}), \
                    patch.object(tools_creator, "_hooks", hooks), \
                    patch.object(tools_artifacts, "ARTIFACTS_FILE", manifest):
                result = tools_creator.generate_video("A waterfall in a forest", duration=12)

            self.assertIn("local ComfyUI workflow", result)
            self.assertIn("/artifact/", result)

    def test_beat_analysis_reports_import_failure_cause(self):
        with patch("builtins.__import__", side_effect=OSError("native audio library unavailable")):
            beat, message = tools_creator._beat_info("song.mp3")

        self.assertIsNone(beat)
        self.assertIn("could not load librosa", message)
        self.assertIn("native audio library unavailable", message)

    def test_generate_video_builds_downloadable_video_from_generated_scenes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = os.path.join(temp_dir, "artifacts.json")
            commands = []

            def fake_run(command, cwd=None, timeout=3600):
                commands.append(command)
                with open(os.path.join(cwd, command[-1]), "wb") as output_file:
                    output_file.write(b"rendered media")
                return ""

            with patch.object(tools_creator, "OUTPUT_DIR", temp_dir), \
                    patch.object(tools_creator, "_media_tool_path", return_value="/usr/local/bin/ffmpeg"), \
                    patch.object(tools_creator, "_run", side_effect=fake_run), \
                    patch.object(tools_creator, "_beat_info", return_value=(None, "no song")), \
                    patch.object(tools_creator, "_hooks", {**tools_creator._hooks, "image": lambda prompt, size: b"png"}), \
                    patch.object(tools_artifacts, "ARTIFACTS_FILE", manifest):
                result = tools_creator.generate_video("Neon city at night", duration=8, bpm=120)

            self.assertIn("beat-aligned scenes", result)
            self.assertIn("/artifact/", result)
            self.assertTrue(any("scene_00.png" in command for command in commands))
            self.assertTrue(any("generated_video.mp4" in command for command in commands))

    def test_yt_dlp_downloads_receive_resolved_ffmpeg_path(self):
        options_seen = []

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options
                options_seen.append(options)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=False):
                return {"duration": 30, "title": "test video"}

            def download(self, urls):
                extension = "mp3" if "song." in self.options["outtmpl"] else "mp4"
                output = self.options["outtmpl"].replace("%(ext)s", extension)
                with open(output, "wb") as downloaded:
                    downloaded.write(b"media")

        fake_yt_dlp = Mock(YoutubeDL=FakeYoutubeDL)
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.dict("sys.modules", {"yt_dlp": fake_yt_dlp}), \
                patch.object(tools_creator, "_media_tool_path", return_value="/opt/homebrew/bin/ffmpeg"):
            tools_creator._download_video("https://example.com/video", temp_dir)
            tools_creator._download_music("https://example.com/song", temp_dir)

        self.assertEqual(len(options_seen), 2)
        self.assertTrue(all(options["ffmpeg_location"] == "/opt/homebrew/bin/ffmpeg"
                            for options in options_seen))

    def test_media_tool_resolver_finds_user_local_binary_outside_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            binary = os.path.join(temp_dir, "ffmpeg")
            with open(binary, "wb") as executable:
                executable.write(b"binary")
            os.chmod(binary, 0o755)
            with patch.object(tools_creator.os.path, "expanduser", return_value=temp_dir), \
                    patch.object(tools_creator.shutil, "which", return_value=None):
                resolved = tools_creator._media_tool_path("ffmpeg")

        self.assertEqual(resolved, binary)

    def test_clip_creation_reports_missing_media_executables(self):
        with patch.object(tools_creator, "_media_tool_path", return_value=None):
            result = tools_creator.create_clips("https://example.com/video.mp4")

        self.assertIn("couldn't find ffmpeg and ffprobe", result)
        self.assertIn("AGATHA_FFMPEG_PATH", result)

    def test_rendered_clip_is_registered_for_download(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "source.mp4")
            output_dir = os.path.join(temp_dir, "outputs")
            manifest = os.path.join(temp_dir, "artifacts.json")
            with open(source, "wb") as video_file:
                video_file.write(b"source")

            def render_clip(src, start, dur, out_name, workdir, *args):
                output = os.path.join(workdir, out_name)
                with open(output, "wb") as clip_file:
                    clip_file.write(b"rendered mp4")
                return output

            with patch.object(tools_creator, "OUTPUT_DIR", output_dir), \
                    patch.object(tools_creator, "_media_tool_path", return_value="/usr/local/bin/ffmpeg"), \
                    patch.object(tools_creator, "_probe", return_value={
                        "duration": 60, "has_audio": False, "width": 1920, "height": 1080,
                    }), \
                    patch.object(tools_creator, "_transcribe", return_value=([], "")), \
                    patch.object(tools_creator, "_pick_moments", return_value=[{
                        "start": 0, "end": 30, "hook": "", "text": "",
                    }]), \
                    patch.object(tools_creator, "_render_clip", side_effect=render_clip), \
                    patch.object(tools_creator, "_captions_for", return_value={}), \
                    patch.object(tools_artifacts, "ARTIFACTS_FILE", manifest):
                result = tools_creator.create_clips(source, num_clips=1)
                artifact_id = result.split("/artifact/", 1)[1].split()[0]
                artifact_path = tools_artifacts.get_artifact_path(artifact_id)

            self.assertIn("Download clip 1: /artifact/", result)
            self.assertTrue(os.path.isfile(artifact_path))

    def test_multipart_upload_rejects_unsupported_extension(self):
        boundary = "test-boundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="payload.exe"\r\n\r\n'
            'blocked\r\n'
            f'--{boundary}--\r\n'
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "Unsupported file type"):
            tools_uploads.parse_multipart_form(f"multipart/form-data; boundary={boundary}", body)

    def test_android_calendar_requires_user_save(self):
        with patch.object(tools_calendar.platform, "system", return_value="Linux"), \
                patch.dict(tools_calendar.os.environ, {"ANDROID_ROOT": "/system"}), \
                patch.object(tools_calendar.shutil, "which", return_value="/system/bin/am"), \
                patch.object(tools_calendar.subprocess, "run", return_value=Mock(returncode=0, stdout="started", stderr="")) as run:
            result = tools_calendar.schedule_meeting("Planning", "2030-06-01 10:30")

        self.assertIn("tap Save", result)
        self.assertIn("content://com.android.calendar/events", run.call_args.args[0])

    def test_portable_calendar_file_is_valid_and_explicit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(tools_calendar.os.path, "expanduser", side_effect=lambda path: temp_dir if path == "~" else path):
                result = tools_calendar._write_ics_event(
                    "Planning, review", datetime(2030, 6, 1, 10, 30), 45,
                    "Import this event", include_alarm=True,
                )

            path = result.split("File saved at ", 1)[1].split(";", 1)[0]
            with open(path, encoding="utf-8") as calendar_file:
                contents = calendar_file.read()
            self.assertIn("SUMMARY:Planning\\, review", contents)
            self.assertIn("DTEND:20300601T111500", contents)
            self.assertIn("BEGIN:VALARM", contents)
            self.assertIn("import it", result)


class NotesAndRemindersTests(unittest.TestCase):
    def test_non_macos_note_is_saved_locally(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_notes.platform, "system", return_value="Windows"), \
                patch.object(tools_notes, "_local_notes_dir", return_value=temp_dir):
            result = tools_notes.create_note("Project notes", "First line\nSecond line")

            note_path = result.rsplit(" ", 1)[-1].rstrip(".")
            with open(note_path, encoding="utf-8") as note_file:
                contents = note_file.read()

        self.assertIn("Created local Markdown note", result)
        self.assertIn("Second line", contents)

    def test_non_macos_reminder_exports_display_alarm(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_calendar.os.path, "expanduser", side_effect=lambda path: temp_dir if path == "~" else path):
            result = tools_reminders.schedule_reminder("Call dentist", "2030-06-01 10:30")

            path = result.split("File saved at ", 1)[1].split(";", 1)[0]
            with open(path, encoding="utf-8") as reminder_file:
                contents = reminder_file.read()

        self.assertIn("BEGIN:VALARM", contents)
        self.assertIn("Call dentist", contents)
        self.assertIn("import it", result)

    def test_macos_reminder_is_pending_import_not_false_success(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_reminders, "_write_ics_event", wraps=tools_calendar._write_ics_event) as write_event, \
                patch.object(tools_calendar.os.path, "expanduser", side_effect=lambda path: temp_dir if path == "~" else path):
            result = tools_reminders.schedule_reminder("Call dentist", "2030-06-01 10:30", "Bring insurance card")

            output_path = result.split("File saved at ", 1)[1].split(";", 1)[0]
            with open(output_path, encoding="utf-8") as reminder_file:
                contents = reminder_file.read()

        self.assertTrue(result.startswith("Not scheduled yet:"))
        self.assertTrue(write_event.call_args.kwargs["include_alarm"])
        self.assertIn("DESCRIPTION:Bring insurance card", contents)


class StartupSafetyTests(unittest.TestCase):
    def test_second_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_instance.platform, "system", return_value="Darwin"), \
                patch.object(tools_instance.os.path, "expanduser", return_value=temp_dir):
            acquired, _ = tools_instance.acquire_instance_lock()
            duplicate, message = tools_instance.acquire_instance_lock()
            self.assertTrue(acquired)
            self.assertFalse(duplicate)
            self.assertIn("Another Agatha instance", message)
            tools_instance._lock_file.close()
            tools_instance._lock_file = None

    def test_dashboard_port_conflict_is_synchronous(self):
        with socket.socket() as server_socket:
            server_socket.bind(("127.0.0.1", 0))
            server_socket.listen()
            port = server_socket.getsockname()[1]
            self.assertFalse(dashboard.start_dashboard(port))


if __name__ == "__main__":
    unittest.main()
