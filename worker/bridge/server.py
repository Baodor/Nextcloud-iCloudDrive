"""Private JSON API and read-only DAV endpoint. Place only on a trusted network."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json
import base64
import os
import re
import secrets
import signal
import threading
from .models import BridgeError, tenant
from .store import Store
from .rclone import Rclone
from .engine import Engine
from .dav import Dav


def read_secret(name):
    filename = os.environ.get(name + "_FILE")
    value = Path(filename).read_text().strip() if filename else os.environ.get(name, "")
    if len(value) < 32:
        raise RuntimeError(f"{name} must be supplied with at least 32 characters.")
    return value


def handler_class(engine, token):
    class Handler(BaseHTTPRequestHandler):
        server_version = "iCloudBridge"
        protocol_version = "HTTP/1.0"

        def setup(self):
            super().setup()
            self.connection.settimeout(125)

        def log_message(self, *_):
            pass  # Never put credentials, file paths or request bodies into HTTP access logs.

        def send_json(self, value, status=200):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="iCloud Drive Bridge"')
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def read_body(self, limit=262144, json_=True):
            if self.headers.get("Transfer-Encoding"):
                raise BridgeError("Chunked requests are not supported.", 400)
            try:
                size = int(self.headers.get("Content-Length", 0))
            except ValueError:
                raise BridgeError("Invalid content length.")
            if not 0 <= size <= limit:
                raise BridgeError("Request body is too large.", 413)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise BridgeError("Incomplete request body.")
            if not json_:
                return raw
            try:
                result = json.loads(raw or b"{}")
            except (ValueError, UnicodeDecodeError):
                raise BridgeError("Invalid JSON request.")
            if not isinstance(result, dict):
                raise BridgeError("Expected a JSON object.")
            return result

        def handle_request(self):
            try:
                parsed = urlsplit(self.path)
                if parsed.path == "/health" and self.command in {"GET", "HEAD"}:
                    healthy = engine.rc.process.poll() is None
                    return self.send_json({"status": "ok" if healthy else "unavailable"}, 200 if healthy else 503)
                if parsed.path.startswith("/dav/"):
                    return engine.dav.proxy(self)
                if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    raise BridgeError("API authentication required.", 401)
                try:
                    uid = base64.b64decode(self.headers.get("X-Bridge-User", ""), validate=True).decode("utf-8")
                except (ValueError, UnicodeDecodeError):
                    raise BridgeError("Invalid user identity.", 401)
                tenant(uid)
                route = parsed.path.removeprefix("/v1/")
                if not parsed.path.startswith("/v1/"):
                    raise BridgeError("Endpoint not found.", 404)
                query = parse_qs(parsed.query)
                data = self.read_body() if self.command in {"POST", "PUT"} else {}
                if route == "state" and self.command == "GET":
                    result = engine.state(uid)
                elif route == "connect/icloud" and self.command == "POST":
                    result = engine.connect_icloud(uid, data)
                elif route == "auth/continue" and self.command == "POST":
                    result = engine.continue_auth(uid, data)
                elif route == "connect/nextcloud" and self.command == "POST":
                    result = engine.connect_nextcloud(uid, data)
                elif route == "folders" and self.command == "GET":
                    result = engine.folders(uid, query.get("side", ["icloud"])[0], query.get("path", [""])[0])
                elif route == "folders" and self.command == "POST":
                    result = engine.mkdir(uid, data.get("path", ""))
                elif route == "jobs" and self.command == "POST":
                    result = engine.save_job(uid, data)
                elif re.fullmatch(r"jobs/[a-f0-9]{32}/run", route) and self.command == "POST":
                    result = engine.queue_run(uid, route.split("/")[1], data.get("action", "run"))
                elif re.fullmatch(r"jobs/[a-f0-9]{32}", route) and self.command in {"PUT", "DELETE"}:
                    result = engine.save_job(uid, data, route.split("/")[1]) if self.command == "PUT" else engine.delete_job(uid, route.split("/")[1])
                elif re.fullmatch(r"runs/[a-f0-9]{32}/stop", route) and self.command == "POST":
                    result = engine.cancel(uid, route.split("/")[1])
                elif route == "dav" and self.command == "GET":
                    result = engine.dav.credentials(uid)
                elif route in {"disconnect/icloud", "disconnect/nextcloud"} and self.command == "POST":
                    result = engine.disconnect(uid, route.split("/")[1])
                elif route == "account" and self.command == "DELETE":
                    result = engine.purge(uid)
                else:
                    raise BridgeError("Endpoint or method not found.", 404)
                self.send_json(result)
            except BridgeError as e:
                self.send_json({"error": str(e)}, e.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.send_json({"error": "Internal bridge error. Check the worker configuration and try again."}, 500)

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PROPFIND = handle_request
    return Handler


def main():
    os.umask(0o077)
    root = Path(os.environ.get("BRIDGE_DATA_DIR", "/data"))
    root.mkdir(parents=True, exist_ok=True)
    token, key = read_secret("BRIDGE_API_TOKEN"), read_secret("BRIDGE_ENCRYPTION_KEY")
    store = Store(root, key)
    rc = Rclone(root, key, secrets.token_urlsafe(32))
    engine = Engine(store, rc, root, os.environ["NEXTCLOUD_URL"])
    engine.dav = Dav(engine, int(os.environ.get("BRIDGE_MAX_DAV_USERS", "20")))
    engine.start()
    server = ThreadingHTTPServer((os.environ.get("BRIDGE_LISTEN", "0.0.0.0"), 8080), handler_class(engine, token))
    server.daemon_threads = True
    def shutdown(*_):
        engine.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print("iCloud Drive Bridge 0.1.0 ready on port 8080", flush=True)
    try:
        server.serve_forever()
    finally:
        engine.stop_event.set()
        engine.runner_thread.join(timeout=100)
        for uid in list(engine.dav.sessions):
            engine.dav.close_user(uid)
        rc.close()
        server.server_close()


if __name__ == "__main__":
    main()
