"""A tiny local web app with known defects, so the browser tests are deterministic and offline."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

PAGE = b"""<!doctype html>
<html><head><title>Fixture shop</title></head>
<body>
  <h1>Fixture shop</h1>
  <img src="/missing-logo.png">
  <button id="add" onclick="document.getElementById('n').textContent = '1 item'">Add to basket</button>
  <p id="n">0 items</p>
  <button onclick="document.getElementById('n').textContent = 'basket cleared'">Delete basket</button>
  <a href="https://example.org/">Partner site</a>
  <script>
    console.error("Cart widget failed: TypeError: cart is undefined");
    fetch("/api/prices").catch(() => {});
  </script>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            status, body, kind = 200, PAGE, "text/html"
        elif self.path == "/api/prices":
            status, body, kind = 500, b'{"error":"boom"}', "application/json"
        else:
            status, body, kind = 404, b"not found", "text/plain"
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
