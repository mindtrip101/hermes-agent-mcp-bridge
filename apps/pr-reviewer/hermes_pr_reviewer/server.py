"""Loopback webhook listener. Request lines are not logged."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hermes_pr_reviewer.service import ReviewService

_MAX_BODY = 2_000_000


class WebhookServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], service: ReviewService) -> None:
        self.service = service
        super().__init__(address, WebhookHandler)


class WebhookHandler(BaseHTTPRequestHandler):
    server: WebhookServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/healthz":
            self._send(200, {"ok": True})
            return
        self._send(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/webhook":
            self._send(404, {"status": "not_found"})
            return
        length = self.headers.get("content-length", "0")
        try:
            size = int(length)
        except ValueError:
            self._send(400, {"status": "unknown"})
            return
        if size < 0 or size > _MAX_BODY:
            self._send(413, {"status": "too_large"})
            return
        body = self.rfile.read(size)
        headers = {key: value for key, value in self.headers.items()}
        status, payload = self.server.service.handle(body, headers)
        self._send(status, payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def serve(service: ReviewService, host: str, port: int) -> None:
    server = WebhookServer((host, port), service)
    try:
        server.serve_forever()
    finally:
        server.server_close()
