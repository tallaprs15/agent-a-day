"""A tiny local page with known accessibility defects, so the browser tests are deterministic and offline."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = b"""<!doctype html>
<html><head><title></title></head>
<body>
  <div>Fixture shop</div>
  <a href="/home"><img src="/logo.png"></a>
  <h3>Deals</h3>
  <a href="/deals">click here</a>
  <input type="text">
  <button><span></span></button>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body, kind = (200, PAGE, "text/html") if self.path == "/" else (404, b"not found", "text/plain")
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output quiet
        pass


@contextmanager
def serve():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
