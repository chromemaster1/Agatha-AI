import json
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import dashboard
import render_gunicorn
import render_start
import tools_auth


class RenderDeploymentTests(unittest.TestCase):
    def test_render_uses_single_worker_gunicorn_gateway_and_private_dashboard_port(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as requirements_file:
            requirements = requirements_file.read()
        with open(os.path.join(root, "Dockerfile"), encoding="utf-8") as dockerfile:
            docker_config = dockerfile.read()
        with open(os.path.join(root, "render.yaml"), encoding="utf-8") as render_file:
            render_config = render_file.read()

        self.assertRegex(requirements, r"(?m)^gunicorn[<=>].*$")
        self.assertIn("gunicorn --bind 0.0.0.0:${PORT:-10000}", docker_config)
        self.assertIn("--workers 1 --threads 8", docker_config)
        self.assertIn("render_gunicorn:application", docker_config)
        self.assertIn("DASHBOARD_HOST", render_config)
        self.assertIn("127.0.0.1", render_config)
        self.assertIn('value: "10001"', render_config)

    def _serve(self, hosted=True):
        mode = patch.object(dashboard, "HOSTED_MODE", hosted)
        mode.start()
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return mode, server, thread

    def _request(self, server, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", server.server_port)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        result = (response.status, response.getheaders(), data)
        connection.close()
        return result

    def _invoke_wsgi(self, method, path, body=b"", headers=None):
        captured = {}
        headers = headers or {}
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": headers.get("Content-Type", ""),
            "wsgi.input": io.BytesIO(body),
        }
        for key, value in headers.items():
            if key.lower() == "content-type":
                continue
            environ["HTTP_" + key.upper().replace("-", "_")] = value

        def start_response(status, response_headers):
            captured["status"] = status
            captured["headers"] = response_headers

        response_body = b"".join(render_gunicorn.application(environ, start_response))
        return captured["status"], dict(captured["headers"]), response_body

    def test_health_endpoint_is_public_and_contains_no_private_state(self):
        mode, server, thread = self._serve()
        try:
            status, _, body = self._request(server, "GET", "/healthz")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            mode.stop()

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"status": "ok"})

    def test_hosted_mode_requires_origin_and_disables_signup(self):
        mode, server, thread = self._serve()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            missing_origin = self._request(
                server, "POST", "/api/auth/login", body="{}",
                headers={"Content-Type": "application/json"})
            signup = self._request(
                server, "POST", "/api/auth/signup", body=json.dumps({"name": "intruder"}),
                headers={"Content-Type": "application/json", "Origin": origin})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            mode.stop()

        self.assertEqual(missing_origin[0], 403)
        self.assertEqual(signup[0], 403)
        self.assertIn(b"provisioned by the service operator", signup[2])

    def test_unauthenticated_session_probe_does_not_clear_active_owner_gate(self):
        mode, server, thread = self._serve()
        dashboard.authenticated_event.set()
        try:
            with patch.object(tools_auth, "is_authenticated", return_value=False):
                status, _, _ = self._request(server, "GET", "/api/session")
            self.assertEqual(status, 200)
            self.assertTrue(dashboard.authenticated_event.is_set())
        finally:
            dashboard.authenticated_event.clear()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            mode.stop()

    def test_hosted_login_sets_secure_cookie(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_auth, "DATA_DIR", temp_dir), \
                patch.object(tools_auth, "ACCOUNT_FILE", os.path.join(temp_dir, "account.json")):
            tools_auth.signup("Render Owner", "owner@example.com", "a-strong-hosted-password")
            mode, server, thread = self._serve()
            origin = f"http://127.0.0.1:{server.server_port}"
            try:
                status, headers, body = self._request(
                    server, "POST", "/api/auth/login",
                    body=json.dumps({"identifier": "owner@example.com",
                                     "password": "a-strong-hosted-password", "remember": False}),
                    headers={"Content-Type": "application/json", "Origin": origin})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                mode.stop()

        cookie = dict(headers).get("Set-Cookie", "")
        self.assertEqual(status, 200)
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(json.loads(body)["account"]["name"], "Render Owner")

    def test_owner_provisioning_is_persistent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_auth, "DATA_DIR", temp_dir), \
                patch.object(tools_auth, "ACCOUNT_FILE", os.path.join(temp_dir, "account.json")), \
                patch.dict(os.environ, {"AGATHA_OWNER_NAME": "Render Owner",
                                        "AGATHA_OWNER_IDENTIFIER": "owner@example.com",
                                        "AGATHA_OWNER_PASSWORD": "a-strong-hosted-password"}), \
                patch("tools_profile.set_name") as set_name:
            render_start.provision_owner()
            with open(tools_auth.ACCOUNT_FILE, encoding="utf-8") as account_file:
                account = json.load(account_file)
            render_start.provision_owner()

        self.assertEqual(account["identifier"], "owner@example.com")
        self.assertTrue(account["password_hash"])
        set_name.assert_called_once_with("Render Owner")

    def test_owner_provisioning_requires_deploy_secrets_on_first_boot(self):
        with patch.object(tools_auth, "account_exists", return_value=False), \
                patch.dict(os.environ, {"AGATHA_OWNER_IDENTIFIER": "", "AGATHA_OWNER_PASSWORD": ""}):
            with self.assertRaisesRegex(RuntimeError, "Set AGATHA_OWNER_IDENTIFIER"):
                render_start.provision_owner()

    def test_render_data_directory_is_used_for_app_state(self):
        with tempfile.TemporaryDirectory() as data_dir:
            environment = os.environ.copy()
            environment["AGATHA_DATA_DIR"] = data_dir
            probe = subprocess.run(
                [sys.executable, "-c", (
                    "import dashboard, tools_artifacts, tools_memory, tools_settings, "
                    "tools_subagents, tools_todo, tools_uploads; "
                    "print('|'.join((dashboard.HISTORY_FILE, tools_artifacts.ARTIFACTS_FILE, "
                    "tools_memory.MEMORY_FILE, tools_settings.SETTINGS_FILE, "
                    "tools_subagents.SUBAGENTS_FILE, tools_todo.TODO_FILE, tools_uploads.UPLOAD_DIR)))"
                )],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env=environment, capture_output=True, text=True, check=True,
            )

        paths = probe.stdout.strip().split("|")
        self.assertEqual(len(paths), 7)
        self.assertTrue(all(os.path.commonpath((data_dir, path)) == data_dir for path in paths))

    def test_gunicorn_gateway_proxies_hosted_login_and_secure_cookie(self):
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(tools_auth, "DATA_DIR", temp_dir), \
                patch.object(tools_auth, "ACCOUNT_FILE", os.path.join(temp_dir, "account.json")), \
                patch.object(dashboard, "HOSTED_MODE", True), \
                patch.object(render_gunicorn, "_DASHBOARD_PORT", 0), \
                patch.object(render_gunicorn, "_start_agent_once"):
            tools_auth.signup("Render Owner", "owner@example.com", "a-strong-hosted-password")
            backend = ThreadingHTTPServer(("127.0.0.1", 0), dashboard._Handler)
            render_gunicorn._DASHBOARD_PORT = backend.server_port
            thread = threading.Thread(target=backend.serve_forever, daemon=True)
            thread.start()
            try:
                status, headers, body = self._invoke_wsgi(
                    "POST", "/api/auth/login",
                    json.dumps({"identifier": "owner@example.com",
                                "password": "a-strong-hosted-password", "remember": False}).encode(),
                    {"Host": "agatha.example.test", "Origin": "https://agatha.example.test",
                     "Content-Type": "application/json"},
                )
            finally:
                backend.shutdown()
                backend.server_close()
                thread.join(timeout=2)

        self.assertTrue(status.startswith("200 "))
        self.assertIn("Secure", headers["Set-Cookie"])
        self.assertEqual(json.loads(body)["account"]["name"], "Render Owner")


if __name__ == "__main__":
    unittest.main()
