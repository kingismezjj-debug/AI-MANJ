"""Serve only unguessable JPEG keyframes. No directory listing, no other files."""

import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/data")
NAME = re.compile(r"^/([A-Za-z0-9_-]{16,}\.jpg)$")
MAX_AGE = 24 * 60 * 60


def sweep() -> None:
    cutoff = time.time() - MAX_AGE
    if not ROOT.is_dir():
        return
    for path in ROOT.glob("*.jpg"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def _sweep_loop() -> None:
    while True:
        sweep()
        time.sleep(600)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        match = NAME.match(self.path.split("?", 1)[0])
        if not match:
            self.send_error(404)
            return
        path = ROOT / match.group(1)
        if not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_sweep_loop, name="frame-sweep", daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
