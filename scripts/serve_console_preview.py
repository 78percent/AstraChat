"""Loopback console with scoped read snapshots and authenticated write controls."""
from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit

from console_data_bridge import BridgeError, fetch_live, manage_live
from console_launcher_control import LauncherControl

DIST = Path(__file__).resolve().parents[1] / "console-ui" / "dist"
LAUNCHER = LauncherControl()


class PreviewHandler(SimpleHTTPRequestHandler):
    def _allowed(self, *, api=False):
        host = self.headers.get('Host', '')
        origin = self.headers.get('Origin')
        return (host in ('127.0.0.1:5178', 'localhost:5178')
                and (origin is None or origin == 'http://' + host)
                and self.headers.get('Sec-Fetch-Site', 'none') in ('none', 'same-origin')
                and (not api or self.headers.get('X-Console-Read') == '1'))

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        is_api = url.path.startswith('/api/')
        if not self._allowed(api=is_api):
            self._json(403, {'error': 'local_origin_required'})
            return
        if url.path == "/__preview_health":
            self._json(200, {'app': 'mitsuha-console', 'version': 3, 'readOnly': False})
            return
        if is_api:
            try:
                if url.path == '/api/launcher' and not url.query:
                    self._json(200, LAUNCHER.status())
                elif url.path == '/api/control/status' and not url.query:
                    self._json(200, manage_live({'op': 'status'}))
                else:
                    self._json(200, fetch_live(url.path, url.query))
            except BridgeError as exc:
                self._json(exc.status, {'error': exc.code})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except Exception:
                self._json(503, {'error': 'snapshot_unavailable'})
            return
        super().do_GET()

    def do_POST(self):
        if not self._allowed(api=True) or self.headers.get('X-Console-Write') != '1':
            self._json(403, {'error': 'local_origin_required'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024 * 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self._json(400, {'error': 'invalid_request'})
                return
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError('invalid_request')
            if self.path == '/api/control':
                value = manage_live(payload)
            elif self.path == '/api/launcher':
                value = LAUNCHER.command(payload)
            else:
                self._json(404, {'error': 'not_found'})
                return
            self._json(200, value)
        except BridgeError as exc:
            self._json(exc.status, {'error': exc.code})
        except (ValueError, UnicodeError):
            self._json(400, {'error': 'invalid_request'})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self._json(503, {'error': 'management_failed'})

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def list_directory(self, path):
        self.send_error(403, "Directory listing disabled")
        return None

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    if not (DIST / "index.html").is_file():
        raise SystemExit("Build console-ui first: npm run build")
    handler = partial(PreviewHandler, directory=str(DIST))
    with ThreadingHTTPServer(("127.0.0.1", 5178), handler) as server:
        server.serve_forever()
