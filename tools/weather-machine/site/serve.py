#!/usr/bin/env python3
"""Minimal static-file server for the Philadelphia Weather Machine front-end.

Pattern A target: this runs as a systemd service on a dedicated port behind the
ALB. The ALB terminates TLS and enforces Okta (authenticate-oidc); the host
security group accepts this port ONLY from the ALB security group. So there is
deliberately no auth, no TLS, and no reverse proxy here — the boundary lives at
the ALB, not in the app.

Zero third-party dependencies (stdlib only), matching the backend's boto3-only
ethos. ThreadingHTTPServer handles the handful of concurrent internal newsroom
users comfortably; the production caveats of http.server (single-threaded,
internet exposure) do not apply behind the ALB+Okta+SG boundary.

If traffic or robustness needs ever outgrow this, the clean upgrade path is to
drop the same static directory behind Caddy or nginx as the systemd service —
no application code changes required.

Usage:
    python3 serve.py [--dir DOCROOT] [--port PORT] [--host HOST]

Environment (overridden by flags):
    WM_DOCROOT   directory to serve (default: this file's directory)
    WM_PORT      port to bind     (default: 8080)
    WM_HOST      address to bind  (default: 127.0.0.1 — front it with the ALB)
"""

import argparse
import os
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class Handler(SimpleHTTPRequestHandler):
    """Static handler with directory listing disabled and a stable health path.

    GET/HEAD `/` serves index.html (also the ALB target-group health check).
    Directory listings are refused so the docroot is never enumerable.
    """

    # No-store on the feed so a poll always sees the freshest synced copy;
    # long-cache the immutable assets.
    def end_headers(self):
        path = self.path.split("?", 1)[0]
        if path.endswith("feed.json"):
            self.send_header("Cache-Control", "no-store")
        elif path.endswith((".css", ".js", ".png", ".svg", ".woff2")):
            self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def list_directory(self, path):  # noqa: D401 — refuse listings
        self.send_error(403, "Directory listing forbidden")
        return None

    def log_message(self, fmt, *args):
        # Concise single-line logs for journald; drop the noisy client address.
        print("web: " + (fmt % args))


def main():
    ap = argparse.ArgumentParser(description="Serve the Weather Machine front-end.")
    ap.add_argument("--dir", default=os.environ.get("WM_DOCROOT", os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WM_PORT", "8080")))
    ap.add_argument("--host", default=os.environ.get("WM_HOST", "127.0.0.1"))
    args = ap.parse_args()

    handler = partial(Handler, directory=args.dir)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"web: serving {args.dir} on http://{args.host}:{args.port} (health: /)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
