"""Gunicorn WSGI gateway for Agatha's existing authenticated dashboard server."""
import http.client
import os
import threading
from http import HTTPStatus


_DASHBOARD_HOST = "127.0.0.1"
_DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "10001"))
_agent_lock = threading.Lock()
_agent_started = False
_agent_error = None

_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _run_agent():
    global _agent_error
    try:
        from render_start import main
        main()
    except BaseException as error:
        _agent_error = error
        print(f"[Render] Agatha runtime stopped: {error}", flush=True)


def _start_agent_once():
    global _agent_started
    with _agent_lock:
        if _agent_started:
            return
        _agent_started = True
        threading.Thread(target=_run_agent, name="agatha-agent", daemon=True).start()


def _request_body(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return None
    return environ["wsgi.input"].read(length)


def application(environ, start_response):
    """Forward WSGI requests to the private server that owns Agatha's stateful UI."""
    _start_agent_once()
    if _agent_error is not None:
        body = f"Agatha failed to start: {_agent_error}".encode("utf-8")
        start_response("503 Service Unavailable", [
            ("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body))),
        ])
        return [body]

    path = environ.get("PATH_INFO", "/")
    query = environ.get("QUERY_STRING", "")
    target = path + (f"?{query}" if query else "")
    headers = {
        key[5:].replace("_", "-").title(): value
        for key, value in environ.items()
        if key.startswith("HTTP_")
    }
    headers["Host"] = environ.get("HTTP_HOST", "localhost")
    if environ.get("CONTENT_TYPE"):
        headers["Content-Type"] = environ["CONTENT_TYPE"]
    if environ.get("CONTENT_LENGTH"):
        headers["Content-Length"] = environ["CONTENT_LENGTH"]

    connection = http.client.HTTPConnection(_DASHBOARD_HOST, _DASHBOARD_PORT, timeout=120)
    try:
        connection.request(environ.get("REQUEST_METHOD", "GET"), target,
                           body=_request_body(environ), headers=headers)
        response = connection.getresponse()
        body = response.read()
        response_headers = [
            (key, value) for key, value in response.getheaders()
            if key.lower() not in _HOP_BY_HOP_HEADERS
        ]
        status = f"{response.status} {response.reason or HTTPStatus(response.status).phrase}"
        start_response(status, response_headers)
        return [body]
    except (OSError, http.client.HTTPException) as error:
        body = f"Agatha dashboard is starting or unavailable: {error}".encode("utf-8")
        start_response("503 Service Unavailable", [
            ("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body))),
            ("Retry-After", "3"),
        ])
        return [body]
    finally:
        connection.close()